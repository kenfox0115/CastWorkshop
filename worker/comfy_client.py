"""
Thin wrapper around ComfyUI's HTTP API, built around your k2 turbo
text-to-image workflow (workflows/k2_txt2img_api.json).

Injects the prompt into node 30:19 ("Text String (User Prompt)"), randomizes
the seed (node 52) so repeat requests don't all return the same image,
submits via /prompt, polls /history for the result, fetches the image
bytes via /view, and releases VRAM via /free once done — per the
architecture doc's Ollama<->ComfyUI boundary rule.

Env vars:
  COMFY_URL - defaults to http://127.0.0.1:8188
"""

import copy
import json
import os
import random
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import requests
from worker.image_prompts import build_image_prompt, build_sequential_image_prompts

PROGRESS_CALLBACK = None


def _comfy_python():
    from scripts.comfy_setup import find_comfy_python
    project = Path(__file__).resolve().parents[1]
    explicit = os.environ.get("COMFY_PYTHON")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = project / path
        return str(path) if path.is_file() else ""
    configured = os.environ.get("COMFYUI_PATH")
    roots = [Path(configured).expanduser()] if configured else []
    roots = [root if root.is_absolute() else project / root for root in roots]
    roots.append(project / "ComfyUI")
    home = Path.home()
    roots.extend((home / "ComfyUI", home / "comfyui", home / "Documents" / "ComfyUI"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        installs = Path(local) / "Comfy-Desktop" / "ComfyUI-Installs"
        roots.extend(installs.glob("*/ComfyUI"))
    for root in roots:
        python = find_comfy_python(root)
        if python:
            return str(python)
    return ""


def prepare_identity_reference(filename: str) -> str:
    """Remove the portrait background before multi-reference scene composition."""
    project = Path(__file__).resolve().parents[1]
    model = Path(os.environ.get(
        "IDENTITY_SEGMENTATION_MODEL",
        project / "models" / "u2net_human_seg.onnx",
    )).expanduser()
    if not model.is_absolute():
        model = project / model
    python = _comfy_python()
    if not model.is_file() or not python:
        raise RuntimeError(
            "Scene-anchored multi-character media requires u2net_human_seg.onnx "
            "and a detected ComfyUI Python environment. Run setup or set "
            "IDENTITY_SEGMENTATION_MODEL and COMFY_PYTHON."
        )
    response = requests.get(
        f"{COMFY_URL}/view", params={"filename": filename, "type": "input"}, timeout=30,
    )
    response.raise_for_status()
    with tempfile.TemporaryDirectory(prefix="castworkshop-identity-") as folder:
        source = Path(folder) / "source.png"
        output = Path(folder) / "isolated.png"
        source.write_bytes(response.content)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [python, str(project / "scripts" / "isolate_identity.py"),
             "--model", str(model), "--input", str(source), "--output", str(output)],
            check=True, timeout=120, env=env, capture_output=True, text=True,
        )
        return upload_bytes_to_input(
            output.read_bytes(), f"identity_isolated_{uuid.uuid4().hex}.png",
        )


def submit_prompt(workflow):
    if PROGRESS_CALLBACK:
        PROGRESS_CALLBACK("submitting", endpoint=COMFY_URL)
    # GUI or an orphaned render may still own the engine after a worker restart.
    queue = requests.get(f"{COMFY_URL}/queue", timeout=10)
    queue.raise_for_status()
    state = queue.json()
    if state.get("queue_running") or state.get("queue_pending"):
        raise RuntimeError("ComfyUI still has work queued; refusing overlapping submission")
    # A timed-out POST may already have been accepted. Never blindly retry it.
    response = requests.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=30)
    response.raise_for_status()
    prompt_id = response.json()["prompt_id"]
    if PROGRESS_CALLBACK:
        classes = {node.get("class_type", "") for node in workflow.values()}
        phase = "rendering_video" if "SaveVideo" in classes else (
            "rendering_voice" if "SaveAudioAdvanced" in classes else "rendering_image")
        PROGRESS_CALLBACK(phase, prompt_id=prompt_id, endpoint=COMFY_URL)
    return prompt_id


def wait_for_images(prompt_id, save_node, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            response.raise_for_status()
            history = response.json()
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(2)
            continue
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI job {prompt_id} failed: {status.get('messages')}")
            images = entry.get("outputs", {}).get(save_node, {}).get("images", [])
            if images:
                return images[0]
            raise RuntimeError(f"ComfyUI job {prompt_id} finished without image output at node {save_node}")
        time.sleep(2)
    raise TimeoutError(f"ComfyUI job {prompt_id} still unresolved after {timeout}s; inspect history before retrying")


def _detect_comfy_url() -> str:
    """Consolidate on the single ComfyUI you actually run (Comfy UI Desktop GUI).
    Probe the GUI-default port 8188 first, then fallbacks. An explicit COMFY_URL
    env override wins and bypasses probing."""
    override = os.environ.get("COMFY_URL")
    if override:
        return override.rstrip("/")
    for port in (8188, 8189, 8190, 8191):
        url = f"http://127.0.0.1:{port}"
        try:
            resp = requests.get(f"{url}/system_stats", timeout=2)
            if resp.ok:
                return url
        except requests.RequestException:
            continue
    return "http://127.0.0.1:8188"  # default fallback (GUI instance)


COMFY_URL = _detect_comfy_url()


PROMPT_NODE_ID = "30:19"
SEED_NODE_ID = "52"
LATENT_NODE_ID = "30:5"
SAVE_NODE_ID = "57"



# LoRA names that embody a character's identity and must STAY loaded in SFW.
KREA2_IDENTITY_LORA_NAMES = ("identity", "anchor", "char", "character")

# LoRA names stripped in SFW mode. NSFW-tuned LoRAs are disabled here; identity
# LoRAs survive SFW. realism_engine is ALSO stripped: vanilla Krea2 puts clothes
# on a naked reference by itself, but realism_engine_krea2_v2 overrides that and
# keeps the subject exposed, so it must be zeroed in SFW (user-verified).
# Matching is substring, lowercase. Identity LoRAs (KREA2_IDENTITY_LORA_NAMES)
# are never stripped.
KREA2_NSFW_LORA_NAMES = ("filterbypass", "knpv", "gonzo", "gonzolomo", "uncensored", "realism_engine")

# Sampler class types that carry a `seed` field (classic uses a PrimitiveInt).
SAMPLER_CLASSES = ("ksampler", "clownsharksampler", "clownsharksampler_beta")

# prompt_id -> discovered SaveImage node id, read by wait_for_*.
_SAVE_NODE_BY_PROMPT = {}


def _find_save_node_id(workflow: dict) -> str:
    """Return this workflow's SaveImage node id, falling back to SAVE_NODE_ID.
    Classic k2 => '57'; custom-unload => '29'."""
    saves = [k for k, v in workflow.items() if v.get("class_type") == "SaveImage"]
    if not saves:
        return SAVE_NODE_ID
    return str(SAVE_NODE_ID) if str(SAVE_NODE_ID) in saves else saves[0]


def _seed_field(workflow: dict):
    """Locate a seed injection point. Returns ('node', id, 'value') for a
    PrimitiveInt node, ('sampler', id, 'seed') for a sampler's own seed, or
    ('random_noise', id, 'noise_seed') for a RandomNoise node (used by the
    reference-blend workflows). Returns None if none exists."""
    for nid, node in workflow.items():
        if node.get("class_type") == "PrimitiveInt":
            return ("node", str(nid), "value")
    for nid, node in workflow.items():
        if (str(node.get("class_type", "")).lower() in SAMPLER_CLASSES
                and "seed" in node.get("inputs", {})):
            return ("sampler", str(nid), "seed")
    for nid, node in workflow.items():
        if (str(node.get("class_type", "")).lower() == "randomnoise"
                and "noise_seed" in node.get("inputs", {})):
            return ("random_noise", str(nid), "noise_seed")
    return None



# Shared Krea2 checkpoint, also used for SFW fallback of custom workflows.
VANILLA_KREA2_UNET = "krea2_turbo_fp8_scaled.safetensors"
NSFW_UNET_NAMES = ("gonzalomo", "gonzo", "gonzolomo")


def apply_sfw_transforms(workflow: dict) -> dict:
    """Full SFW workflow transform, in place:
      1) Disable style/NSFW LoRA layers by zeroing their strength. Only
         NSFW-TUNED LoRAs (filterbypass / knpv / gonzo / uncensored /
         realism_engine — see KREA2_NSFW_LORA_NAMES) are stripped; identity
         LoRAs survive SFW. realism_engine is stripped too: without it vanilla
         Krea2 puts clothes on a naked reference (user-verified). Also disables
         the "Enable LoRA?" boolean gate in the custom-unload graphs.
      2) Swap any UNETLoader whose model name is gonzolomo/gonzo back to the
         vanilla krea2 model, so NSFW-tuned checkpoints never load in SFW.
    Returns the same dict for convenience."""
    lora_gate_titles = ("enable lora", "enable lora?", "lora enabled",
                       "enable_loras", "lora switch", "loras")
    for node_id, node in workflow.items():
        ct = str(node.get("class_type", "")).lower()
        inputs = node.get("inputs", {})
        if ct in ("loraloader", "loraloadermodelonly"):
            name = str(inputs.get("lora_name", "")).lower()
            if any(tok in name for tok in KREA2_NSFW_LORA_NAMES):
                # NSFW-tuned LoRA: strip in SFW. Realism/identity/quality survive.
                inputs["strength_model"] = 0.0
                if "strength_clip" in inputs:
                    inputs["strength_clip"] = 0.0
        elif ct == "unetloader":
            unet = str(inputs.get("unet_name", "")).lower()
            if any(tok in unet for tok in NSFW_UNET_NAMES):
                inputs["unet_name"] = VANILLA_KREA2_UNET
        elif ct == "primitiveboolean":
            title = str(node.get("_meta", {}).get("title", "")).lower()
            if any(tok in title for tok in lora_gate_titles):
                inputs["value"] = False
    return workflow
def _strip_if_sfw(workflow: dict) -> dict:
    """Apply the full SFW transform only when the global content mode is SFW."""
    if _content_mode() == "sfw":
        return apply_sfw_transforms(workflow)
    return workflow


# Module-level so comfy_client can be driven without importing worker (tests).
_content_mode_state = None


def set_content_mode(mode: str):
    """Set the content mode module-global (worker calls this per job)."""
    global _content_mode_state
    _content_mode_state = mode if mode in ("sfw", "nsfw") else "sfw"


def _content_mode() -> str:
    """Read the content mode: module-global (set by worker) > CONTENT_MODE env > SFW."""
    if _content_mode_state:
        return _content_mode_state
    forced = os.environ.get("CONTENT_MODE")
    return forced if forced in ("sfw", "nsfw") else "sfw"


# Aspect mode ('vertical' | 'cinematic'), module-global set by the worker per-job,
# exactly like content mode. Only 'cinematic' rewrites resolution; vertical = no-op.
_aspect_mode_state = None


def set_aspect_mode(mode: str):
    """Set the aspect mode module-global (worker calls this per job)."""
    global _aspect_mode_state
    _aspect_mode_state = mode if mode in ("cinematic", "vertical") else "vertical"


def _aspect_mode() -> str:
    """Read aspect mode: module-global > ASPECT_MODE env > 'vertical'."""
    if _aspect_mode_state:
        return _aspect_mode_state
    forced = os.environ.get("ASPECT_MODE")
    return forced if forced in ("cinematic", "vertical") else "vertical"


# Map aspect modes to ResolutionSelector.aspect_ratio widget values. These are the
# exact enum strings ComfyUI's ResolutionSelector node accepts (confirmed via
# /object_info). 'vertical' = no-op (workflow default is already portrait).
CINEMATIC_ASPECT = "16:9 (Widescreen)"
_VERTICAL_ASPECT = None  # None => leave the workflow's existing (portrait) ratio.


def apply_aspect_mode(workflow: dict) -> dict:
    """Rewrite every ResolutionSelector node's aspect_ratio to widescreen when the
    aspect mode is cinematic; no-op (keep the workflow's portrait default) when
    vertical. In place, like apply_sfw_transforms. Returns the workflow dict.

    Both image workflows (node 71) and video workflows (node 115) use a
    ResolutionSelector for output dimensions, so a single helper covers them.
    """
    target = CINEMATIC_ASPECT if _aspect_mode() == "cinematic" else _VERTICAL_ASPECT
    if target is None:
        return workflow
    for node_id, node in workflow.items():
        ct = str(node.get("class_type", ""))
        if ct == "ResolutionSelector" and "aspect_ratio" in node.get("inputs", {}):
            node["inputs"]["aspect_ratio"] = target
    return workflow


def ensure_unload_before_save(workflow: dict) -> dict:
    """Guarantee an UnloadAllModels node sits right before every SaveImage, in
    place, so ComfyUI frees VRAM for Ollama instead of the two competing on a
    16GB card. The Gonzo _unload workflows already have it; this inserts it for
    workflows (e.g. k2_txt2img_api) that don't. Runs on
    the in-memory copy, never the on-disk file. Returns the workflow dict."""
    # All existing node ids (so we pick a collision-free id for inserted nodes).
    used = {str(k) for k in workflow.keys()}
    next_id = 0
    while str(next_id) in used:
        next_id += 1

    for node_id, node in list(workflow.items()):
        if node.get("class_type") != "SaveImage":
            continue
        src = node.get("inputs", {}).get("images")
        # If the images already come from an UnloadAllModels node, it's handled.
        if (isinstance(src, (list, tuple)) and src
                and workflow.get(str(src[0]), {}).get("class_type") == "UnloadAllModels"):
            continue
        # Insert UnloadAllModels before this SaveImage: take its current source
        # as the unload node's pass-through input, then point SaveImage at it.
        uid = str(next_id)
        while uid in used:
            next_id += 1
            uid = str(next_id)
        used.add(uid)
        workflow[uid] = {
            "inputs": {"value": src},
            "class_type": "UnloadAllModels",
            "_meta": {"title": "Unload ALL Models"},
        }
        node["inputs"]["images"] = [uid, 0]
    return workflow


def configure_prompt_enhancement(workflow, adult_mode):
    """Fix the old enhancer's wardrobe assumption only for adult submissions."""
    if adult_mode:
        for node in workflow.values():
            inputs = node.get("inputs", {})
            for key, value in inputs.items():
                if isinstance(value, str) and "Assume clothing covers genitals and intimate anatomy." in value:
                    inputs[key] = value.replace(
                        "Assume clothing covers genitals and intimate anatomy.",
                        "Preserve the requested final wardrobe or nudity state for consenting adults; "
                        "do not add covering garments. Sexual content involving minors is prohibited.")
    return workflow


def submit_workflow(prompt_text: str, batch_size: int = 1,
                     workflow_filename: str = "k2_txt2img_api.json",
                     skip_unload: bool = False,
                     apply_aspect: bool = True,
                     force_sfw: bool = False) -> str:
    """Injects the prompt + a fresh random seed + batch size, submits, returns
    prompt_id. `workflow_filename` selects the txt2img workflow file (defaults
    to the classic k2_txt2img). When `skip_unload=True`, the UnloadAllModels
    node is NOT injected before SaveImage — used to keep the models warm across
    a sequence of renders (e.g. look candidates); the LAST render of the sequence
    should use skip_unload=False so VRAM is freed at the end.
    `apply_aspect=True` rewrites ResolutionSelector to the current /aspect mode
    (cinematic -> 16:9) for final images. Set False for intermediate renders that
    must stay portrait (look candidates), independent of /aspect.
    `force_sfw=True` applies the SFW workflow transform regardless of the active
    chat mode; character look candidates use this to establish a clean identity
    reference before later media follows the user's selected content mode."""
    with open(resolve_workflow_path(workflow_filename), encoding="utf-8") as f:
        workflow = json.load(f)
    workflow = copy.deepcopy(workflow)
    if force_sfw:
        apply_sfw_transforms(workflow)
    else:
        _strip_if_sfw(workflow)
    configure_prompt_enhancement(workflow, not force_sfw and _content_mode() == "nsfw")
    if apply_aspect:
        apply_aspect_mode(workflow)
    if not skip_unload:
        ensure_unload_before_save(workflow)
    workflow[PROMPT_NODE_ID]["inputs"]["value"] = prompt_text
    if LATENT_NODE_ID in workflow:
        workflow[LATENT_NODE_ID]["inputs"]["batch_size"] = batch_size
    seed_slot = _seed_field(workflow)
    if seed_slot is None:
        raise RuntimeError(
            f"workflow {workflow_filename} has no usable seed source")
    _, node_id, field = seed_slot
    workflow[node_id]["inputs"][field] = random.randint(0, 2**31 - 1)
    save_node = _find_save_node_id(workflow)

    prompt_id = submit_prompt(workflow)
    _SAVE_NODE_BY_PROMPT[prompt_id] = save_node
    return prompt_id


def wait_for_results(prompt_id: str, timeout: int = 300, poll_interval: float = 2.0) -> list:
    """Polls /history until this job shows up with output images.
    Returns the full list of image info dicts (one per batch item)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            resp.raise_for_status()
            history = resp.json()
        except (requests.exceptions.RequestException, ValueError):
            # Transient poll blip (server busy/restarting): keep trying until deadline.
            time.sleep(poll_interval)
            continue

        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            outputs = entry.get("outputs", {})
            sv = _SAVE_NODE_BY_PROMPT.get(prompt_id, SAVE_NODE_ID)
            images = outputs.get(sv, {}).get("images", [])
            if images:
                return images
            if status.get("status_str") == "error":
                # Surface the REAL node error instead of a misleading
                # "produced no images" (an errored run yields empty outputs).
                raise RuntimeError(
                    f"ComfyUI job {prompt_id} errored: {status.get('messages')}")
            raise RuntimeError(f"ComfyUI job {prompt_id} completed but produced no images")

        time.sleep(poll_interval)

    raise TimeoutError(f"ComfyUI job {prompt_id} did not complete within {timeout}s")


def wait_for_result(prompt_id: str, timeout: int = 300, poll_interval: float = 2.0) -> dict:
    """Single-image convenience wrapper around wait_for_results."""
    return wait_for_results(prompt_id, timeout, poll_interval)[0]


def fetch_image_bytes(image_info: dict) -> bytes:
    resp = requests.get(
        f"{COMFY_URL}/view",
        params={
            "filename": image_info["filename"],
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def _gpu_mem():
    """Backend-reported memory usage in MiB, on any supported ComfyUI host."""
    try:
        response = requests.get(f"{COMFY_URL}/system_stats", timeout=5)
        response.raise_for_status()
        stats = response.json()
        system = stats.get("system", {})
        ram = (system["ram_total"] - system["ram_free"]) // (1024 * 1024) if "ram_total" in system and "ram_free" in system else None
        devices = stats.get("devices", [])
        device = devices[0] if devices else {}
        vram = (device["vram_total"] - device["vram_free"]) // (1024 * 1024) if "vram_total" in device and "vram_free" in device else None
        return vram, ram
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None, None


def free_vram():
    """Release VRAM after the batch. Not needed between consecutive ComfyUI
    stages, but matters at the Ollama<->ComfyUI boundary per the architecture doc.
    Logs a VRAM/RAM delta around the /free call to diagnose model-handoff leaks."""
    before = _gpu_mem()
    try:
        requests.post(
            f"{COMFY_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        print(f"[comfy] free_vram warning: {e}")
    after = _gpu_mem()
    if before != (None, None) or after != (None, None):
        print(f"[telemetry] free_vram VRAM {before[0]}MB -> {after[0]}MB | RAM {before[1]}MB -> {after[1]}MB")


def generate_image(prompt_text: str) -> bytes:
    """Convenience wrapper: submit -> wait -> fetch -> free. Returns raw image bytes."""
    filename = _NEWDEFAULTS_2CHAR_REF[(_aspect_mode(), _content_mode())]
    return fetch_image_bytes(_generate_identity_image(filename, prompt_text))


def upload_bytes_to_input(image_bytes: bytes, filename: str = "upload.png") -> str:
    """Uploads bytes already in hand (e.g. a just-generated scene image)
    directly to ComfyUI's /input folder. Simpler than promote_output_to_input
    when we already have the bytes and don't need to re-fetch them."""
    resp = requests.post(
        f"{COMFY_URL}/upload/image",
        files={"image": (filename, image_bytes)},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["name"]


def promote_output_to_input(filename: str, subfolder: str = "") -> str:
    """A candidate image lives in ComfyUI's /output folder (from SaveImage) and
    can't be picked up by LoadImage, which reads /input. Fetches the bytes and
    re-uploads them via /upload/image so they're usable as a future reference."""
    resp = requests.get(
        f"{COMFY_URL}/view",
        params={"filename": filename, "subfolder": subfolder, "type": "output"},
        timeout=30,
    )
    resp.raise_for_status()
    upload = requests.post(
        f"{COMFY_URL}/upload/image",
        files={"image": (filename, resp.content)},
        timeout=30,
    )
    upload.raise_for_status()
    return upload.json()["name"]


def upload_image_bytes_to_input(data: bytes, filename: str = "scene_anchor.png") -> str:
    """Upload an application-owned image as a durable ComfyUI input reference."""
    upload = requests.post(
        f"{COMFY_URL}/upload/image",
        files={"image": (filename, data, "image/png")},
        timeout=30,
    )
    upload.raise_for_status()
    return upload.json()["name"]


def _is_identity_workflow(filename):
    with open(resolve_workflow_path(filename), encoding="utf-8") as source:
        return any(node.get("class_type") == "Krea2EditModelPatch" for node in json.load(source).values())


def _generate_identity_image(filename, instruction, reference_a=None, reference_b=None):
    """Bind the tested native Krea2 edit graph without changing sampler settings."""
    filename = resolve_identity_workflow(filename)
    with open(resolve_workflow_path(filename), encoding="utf-8") as source:
        workflow = json.load(source)
    required = {"112": "ClownsharKSampler_Beta", "72": "LoadImage", "90": "LoadImage",
                "79": "Krea2EditModelPatch", "84": "Krea2EditGroundedEncode",
                "85": "Krea2EditGroundedEncode", "83": "ResolutionSelector",
                "111": "LoraLoaderModelOnly"}
    if any(workflow.get(key, {}).get("class_type") != kind for key, kind in required.items()):
        raise ValueError(f"{filename}: incompatible native Krea2 identity-edit node layout")
    target_latent = workflow['112']['inputs'].get('latent_image')
    if (target_latent != ['82', 0]
            or workflow['79']['inputs'].get('target_latent') != target_latent
            or workflow.get('82', {}).get('class_type') != 'EmptySD3LatentImage'):
        raise ValueError(f"{filename}: target_latent must retain the approved empty-latent connection")
    workflow["84"]["inputs"]["prompt"] = instruction
    workflow["85"]["inputs"]["prompt"] = ""
    workflow["112"]["inputs"]["seed"] = random.randint(0, 2**31 - 1)
    workflow["83"]["inputs"]["aspect_ratio"] = (
        "16:9 (Widescreen)" if _aspect_mode() == "cinematic" else "9:16 (Portrait Widescreen)")
    if "snofs" not in workflow["111"]["inputs"]["lora_name"].lower():
        raise ValueError(f"{filename}: expected SNOFS LoRA at node 111")
    workflow["111"]["inputs"]["strength_model"] = 1 if _content_mode() == "nsfw" else 0
    if reference_a:
        workflow["72"]["inputs"]["image"] = reference_a
    else:
        workflow["112"]["inputs"]["model"] = workflow["79"]["inputs"]["model"]
        for key in ("72", "73", "79"):
            del workflow[key]
        for key in ("84", "85"):
            workflow[key]["inputs"].pop("image", None)
    if reference_b:
        if not reference_a:
            raise ValueError("Second reference requires a primary reference")
        workflow["90"]["inputs"]["image"] = reference_b
    else:
        del workflow["90"]
        if "79" in workflow:
            workflow["79"]["inputs"].pop("source_image_b", None)
        for key in ("84", "85"):
            workflow[key]["inputs"].pop("image_b", None)
    _strip_if_sfw(workflow)
    ensure_unload_before_save(workflow)
    save_node = _find_save_node_id(workflow)
    print(f"[comfy_client] identity image workflow={filename}, mode={_content_mode()}, aspect={_aspect_mode()}")
    print("[comfy_client] identity settings " + json.dumps({
        "seed": workflow["112"]["inputs"]["seed"],
        "steps": workflow["112"]["inputs"]["steps"],
        "cfg": workflow["112"]["inputs"]["cfg"],
        "sampler_class": workflow["112"]["class_type"],
        "sampler_name": workflow["112"]["inputs"]["sampler_name"],
        "scheduler": workflow["112"]["inputs"]["scheduler"],
        "grounding_px": workflow["84"]["inputs"].get("grounding_px"),
        "identity_strength": workflow["71"]["inputs"].get("strength_model"),
        "snofs_strength": workflow["111"]["inputs"]["strength_model"],
        "ref_boost": workflow.get("79", {}).get("inputs", {}).get("ref_boost"),
        "ref_boost_a": workflow.get("79", {}).get("inputs", {}).get("ref_boost_a"),
    }))
    prompt_id = submit_prompt(workflow)
    _SAVE_NODE_BY_PROMPT[prompt_id] = save_node
    image = wait_for_images(prompt_id, save_node)
    free_vram()
    return image


def resolve_identity_workflow(workflow_filename=None):
    """Only native identity-edit graphs may serve character image editing."""
    baseline = _NEWDEFAULTS_2CHAR_REF[(_aspect_mode(), _content_mode())]
    if (workflow_filename and Path(workflow_filename).name not in _LEGACY_IDENTITY_PRESETS
            and _is_identity_workflow(workflow_filename)):
        return workflow_filename
    if workflow_filename and workflow_filename != baseline:
        print(f"[comfy_client] ignoring legacy character identity override {workflow_filename}; using {baseline}")
    return baseline


def generate_edit_image(reference_filename: str, instruction_text: str,
                        workflow_filename: str = None,
                        apply_aspect: bool = True) -> bytes:
    """Character identity edit. Generic non-reference generation is separate."""
    filename = resolve_identity_workflow(workflow_filename)
    return fetch_image_bytes(_generate_identity_image(
        filename, build_image_prompt(instruction_text, adult_mode=_content_mode() == 'nsfw'), reference_filename))


def _expand_image_override_references(workflow, scene_reference=False):
    """Extend a compatible single-reference Qwen graph without changing its model."""
    encoders = [node for node in workflow.values()
                if node.get("class_type") == "TextEncodeQwenImageEditPlus"]
    if (len(encoders) != 1 or workflow.get("69", {}).get("class_type") != "LoadImage"
            or encoders[0].get("inputs", {}).get("image1") != ["69", 0]):
        raise ValueError("Image override must use one TextEncodeQwenImageEditPlus with image1 from LoadImage 69")
    inputs = encoders[0]["inputs"]
    if "image2" not in inputs:
        if "104" in workflow:
            raise ValueError("Image override node 104 is occupied; cannot add a second reference")
        workflow["104"] = {"class_type": "LoadImage", "inputs": {"image": ""}}
        inputs["image2"] = ["104", 0]
    if scene_reference and "image3" not in inputs:
        if "150" in workflow:
            raise ValueError("Image override node 150 is occupied; cannot add a scene reference")
        workflow["150"] = {"class_type": "LoadImage", "inputs": {"image": ""}}
        inputs["image3"] = ["150", 0]
    expected_b = "102" if workflow.get("102", {}).get("class_type") == "LoadImage" else "104"
    if inputs.get("image2") != [expected_b, 0] or (scene_reference and inputs.get("image3") != ["150", 0]):
        raise ValueError("Image override has incompatible reference wiring; expected image2 from 102/104 and image3 from 150")


def generate_two_char_edit_image(reference_a: str, reference_b: str,
                                  instruction_text: str,
                                  workflow_filename: str = None,
                                  apply_aspect: bool = True,
                                  scene_reference: str = None) -> dict:
    """Compose two identities; default scene jobs use sequential native insertion.

    Explicit compatible workflow overrides retain their reference wiring.
    """
    if scene_reference and (not workflow_filename or _is_identity_workflow(workflow_filename)):
        return generate_two_character_scene_image(
            reference_a, reference_b, scene_reference,
            "the first character", "the second character", instruction_text,
        )
    explicit_override = bool(workflow_filename)
    if not workflow_filename:
        workflow_filename = _NEWDEFAULTS_2CHAR_REF[(_aspect_mode(), _content_mode())]
    template_path = resolve_workflow_path(workflow_filename)
    if _is_identity_workflow(workflow_filename):
        return _generate_identity_image(workflow_filename, instruction_text, reference_a, reference_b)
    with open(template_path, encoding="utf-8") as f:
        workflow = json.load(f)
    workflow = copy.deepcopy(workflow)
    if explicit_override:
        _expand_image_override_references(workflow, bool(scene_reference))
    _strip_if_sfw(workflow)
    if apply_aspect:
        apply_aspect_mode(workflow)
    ensure_unload_before_save(workflow)

    # The curated SFW templates use 102; the other templates use 104.
    # Validate the binding before submission rather than guessing by node order.
    image_b_nodes = [node_id for node_id in (TWOCHAR_IMAGE_B_NODE, "102")
                     if workflow.get(node_id, {}).get("class_type") == "LoadImage"]
    if len(image_b_nodes) != 1:
        raise ValueError(f"{workflow_filename}: expected one second-character LoadImage "
                         f"node (104 or 102), found {image_b_nodes}")
    scene_node = workflow.get(TWOCHAR_SCENE_NODE)
    if scene_reference:
        if not scene_node or scene_node.get("class_type") != "LoadImage":
            raise ValueError(f"{workflow_filename}: missing scene-reference LoadImage node 150")
        # Qwen Image Edit treats image1 as the primary edit canvas. Keep the
        # environment in that privileged slot and use image2/image3 only as
        # identity references; putting character A first allowed both identity
        # photo backgrounds to overwrite large regions of the scene anchor.
        isolated_a = prepare_identity_reference(reference_a)
        isolated_b = prepare_identity_reference(reference_b)
        workflow[TWOCHAR_IMAGE_A_NODE]["inputs"]["image"] = scene_reference
        workflow[image_b_nodes[0]]["inputs"]["image"] = isolated_a
        scene_node["inputs"]["image"] = isolated_b
    elif scene_node:
        raise ValueError(f"{workflow_filename}: scene-reference graph requires scene_reference")
    else:
        workflow[TWOCHAR_IMAGE_A_NODE]["inputs"]["image"] = reference_a
        workflow[image_b_nodes[0]]["inputs"]["image"] = reference_b
    workflow[TWOCHAR_EDIT_PROMPT_NODE]["inputs"]["value"] = instruction_text
    print(f"[comfy_client] 2-char image instruction (first 300): {instruction_text[:300]}")

    seed_slot = _seed_field(workflow)
    if seed_slot is not None:
        _, node_id, field = seed_slot
        workflow[node_id]["inputs"][field] = random.randint(0, 2**31 - 1)

    prompt_id = submit_prompt(workflow)
    save_node = _find_save_node_id(workflow)
    _SAVE_NODE_BY_PROMPT[prompt_id] = save_node

    image = wait_for_images(prompt_id, save_node)
    free_vram()
    return image


def generate_character_scene_image(character_reference: str, scene_reference: str,
                                   instruction_text: str, apply_aspect: bool = True,
                                   assembled_prompt: bool = False) -> bytes:
    """Compose one character with a character-free environment reference."""
    filename = _NEWDEFAULTS_2CHAR_REF[(_aspect_mode(), _content_mode())]
    if _is_identity_workflow(filename):
        if not assembled_prompt:
            instruction_text = build_image_prompt(instruction_text,
                identities=((2, "the character"),), scene_index=1, adult_mode=_content_mode() == 'nsfw')
        return fetch_image_bytes(_generate_identity_image(filename, instruction_text,
            scene_reference, prepare_identity_reference(character_reference)))
    image = generate_two_char_edit_image(
        character_reference, scene_reference, instruction_text,
        apply_aspect=apply_aspect,
    )
    return fetch_image_bytes(image)


def generate_two_character_scene_image(first_reference, second_reference, scene_reference,
                                       first_name, second_name, action, progress=None, latest_request=None,
                                       character_metadata=None):
    """Sequential native insertion for images and video opening frames.

    Reuse the working first-stage entry point. Its saved output remains in ComfyUI;
    an in-memory HTTP transfer makes a unique input copy available for stage two.
    """
    filename = _NEWDEFAULTS_2CHAR_REF[(_aspect_mode(), _content_mode())]
    first_prompt, second_prompt = build_sequential_image_prompts(action, first_name, second_name,
        latest_request=latest_request, adult_mode=_content_mode() == 'nsfw',
        character_metadata=character_metadata)
    def record(stage, prompt, **details):
        print(f"[comfy_client] TWO_CHARACTER PASS {stage}: first={first_name}, "
              f"second={second_name}, workflow={filename}, prompt={prompt}")
        if progress:
            progress(phase="rendering_image", image_prompt=prompt,
                     image_pass=stage, image_passes=2, **details)
    record(1, first_prompt, image_prompt_pass_1=first_prompt)
    intermediate_bytes = generate_character_scene_image(first_reference, scene_reference, first_prompt, assembled_prompt=True)
    intermediate = upload_image_bytes_to_input(
        intermediate_bytes, f"two_character_pass1_{uuid.uuid4().hex}.png")
    print(f"[comfy_client] TWO_CHARACTER PASS 1 intermediate input={intermediate}")
    record(2, second_prompt, image_prompt_pass_2=second_prompt,
           intermediate_image={"filename": intermediate, "type": "input", "subfolder": ""})
    # Do not send this prompt through the normal single-character builder: image 1
    # already contains the first character, whose preservation is stage-specific.
    result = _generate_identity_image(filename, second_prompt, intermediate,
                                      prepare_identity_reference(second_reference))
    return result


VIDEO_IMAGE_NODE_ID = "114"
VIDEO_PROMPT_NODE_ID = "105:104"
VIDEO_SEED_NODE_ID = "105:15"
VIDEO_DURATION_NODE_ID = "105:111"
VIDEO_SAVE_NODE_ID = "92"
VIDEO_OUTPUT_KEYS = ("videos", "gifs", "images")  # uncertain which ComfyUI uses; check all

# MiniMax H3 Reference-to-Video (uses an identity image + a voice reference).
REF2V_IMAGE_NODE_ID = "137"      # LoadImage
REF2V_AUDIO_NODE_ID = "141"      # LoadAudio
REF2V_PROMPT_NODE_ID = "138"     # PrimitiveStringMultiline
REF2V_SEED_NODE_ID = "129"       # RandomNoise
REF2V_DURATION_NODE_ID = "132"   # PrimitiveFloat
REF2V_RES_NODE_ID = "115"        # ResolutionSelector
REF2V_SAVE_NODE_ID = "92"        # SaveVideo
WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), "workflows")

# --- Two-character constants (multi-char image + video) ---
# 2-char Krea2 reference image workflow node IDs
TWOCHAR_IMAGE_A_NODE = "69"          # LoadImage -- image1 / primary canvas
TWOCHAR_IMAGE_B_NODE = "104"         # LoadImage -- image2
TWOCHAR_SCENE_NODE = "150"           # LoadImage -- image3
TWOCHAR_EDIT_PROMPT_NODE = "30:19"   # PrimitiveStringMultiline -- scene prompt
TWOCHAR_SEED_NODE = "30:58"          # ClownsharKSampler_Beta -> .seed
TWOCHAR_SAVE_NODE = "29"             # SaveImage
TWOCHAR_RES_NODE = "71"              # ResolutionSelector

# 2-char H3 video workflow node IDs (extend REF2V_* with extra refs)
TWOCHAR_REF_IMAGE_B = "143"          # LoadImage -- char B identity -> ref_image_1
TWOCHAR_REF_SCENE   = "145"          # LoadImage -- scene/composition -> ref_image_2
TWOCHAR_REF_AUDIO_B = "146"          # LoadAudio -- char B voice -> ref_audio_1

# 2-char workflow file names (by content mode for image, by aspect for H3)
IDENTITY_WORKFLOW = "krea2_identity_edit_clownshark.json"
_LEGACY_IDENTITY_PRESETS = {
    "krea2_identity_edit_SNOFS_unload.json",
    *(f"krea2_identity_edit_{aspect}_{mode}.json"
      for aspect in ("vertical", "cinematic") for mode in ("sfw", "nsfw")),
}
_NEWDEFAULTS_2CHAR_REF = {
    (aspect, mode): IDENTITY_WORKFLOW
    for aspect in ("vertical", "cinematic") for mode in ("sfw", "nsfw")
}

_NEWDEFAULTS_2CHAR_H3 = {
    "vertical":   "presets/minimax_h3_2char_ref2v_vertical.json",
    "cinematic":  "presets/minimax_h3_2char_ref2v_widescreen.json",
}


def resolve_workflow_path(filename: str) -> str:
    """Resolve a workflow filename (or absolute path) against the workflows/
    folder. Falls back to the filename verbatim if no file matches, so an
    absolute path or an already-valid path passes straight through.

    Raises if filename is None/empty: a generated-media step (image, voice, or
    video) must always have a real workflow template to load — passing None here
    used to blow up later as `TypeError: expected str, bytes or os.PathLike
    object, not NoneType` (callers must translate curated keys to a filename)."""
    if not filename:
        raise ValueError("workflow filename is required (got None/empty)")
    if os.path.isabs(filename) and os.path.exists(filename):
        return filename
    candidate = os.path.join(WORKFLOWS_DIR, filename)
    if os.path.exists(candidate):
        return candidate
    return filename


def workflow_is_ref2v(filename: str) -> bool:
    """True if the given workflow file uses MiniMaxH3ReferenceToVideo (character
    speaks via a voice reference) rather than a plain ImageToVideo node.
    Used to route auto-discovered video workflow files to the right function."""
    try:
        p = resolve_workflow_path(filename)
        with open(p) as f:
            wf = json.load(f)
    except (OSError, ValueError):
        return True  # default to R2V on any read problem
    return any("ReferenceToVideo" in v.get("class_type", "") for v in wf.values())


REF2V_ASPECT_RATIO = "9:16 (Portrait Widescreen)"
# ~12% more VRAM headroom vs 0.9 MP — lower to reduce the risk of the GPU-loss /
# CUDA-unknown-error crashes that hit R2V renders hard (RTX 5060 Ti 16GB, VRAM was
# pinning ~15.9/16.3GB on 0.9MP vertical renders). 0.65MP @ 9:16 ≈ 720x1100.
# Instagram Reels-style vertical output (9:16). ~0.9 MP ≈ 720x1280 vertical —
# high enough to look like a reel, low enough to render in a few minutes
# (1080x1920 / 2 MP took 20+ min — too slow for the interactive flow).
REF2V_MEGAPIXELS = 0.65


def _video_resolution():
    """Return (aspect_ratio, megapixels) for the current aspect mode.

    Vertical (default) = the existing 9:16 portrait Reels-style output.
    Cinematic = 16:9 widescreen. Megapixels stay conservative (0.65) so the
    16GB RTX 5060 Ti keeps VRAM headroom on both aspects.
    """
    if _aspect_mode() == "cinematic":
        return ("16:9 (Widescreen)", 0.65)
    return (REF2V_ASPECT_RATIO, REF2V_MEGAPIXELS)


def generate_video(reference_filename: str, prompt_text: str, duration_seconds: float = 6.0,
                   workflow_filename: str = "presets/minimax_h3_ref2v_vertical.json") -> bytes:
    """Image-to-video with audio. reference_filename must already be in
    ComfyUI's /input folder (e.g. a character's promoted base_image_path).
    Identity comes from the model actually starting from that frame, not
    from a text instruction — so prompt_text should just describe the
    motion/action, not an identity-preservation wrapper.
    `workflow_filename` may name a different i2v workflow (same node schema)."""
    with open(resolve_workflow_path(workflow_filename), encoding="utf-8") as f:
        workflow = json.load(f)
    workflow = copy.deepcopy(workflow)

    workflow[VIDEO_IMAGE_NODE_ID]["inputs"]["image"] = reference_filename
    workflow[VIDEO_PROMPT_NODE_ID]["inputs"]["prompt"] = prompt_text
    workflow[VIDEO_DURATION_NODE_ID]["inputs"]["value"] = duration_seconds
    workflow[VIDEO_SEED_NODE_ID]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)
    # Match the ref2v path: Reels-style vertical (9:16) by default; widescreen
    # (16:9) when /aspect is cinematic. Node 115 (ResolutionSelector) drives both.
    wf_aspect, wf_mp = _video_resolution()
    workflow[REF2V_RES_NODE_ID]["inputs"]["aspect_ratio"] = wf_aspect
    workflow[REF2V_RES_NODE_ID]["inputs"]["megapixels"] = wf_mp

    prompt_id = submit_prompt(workflow)

    deadline = time.time() + 1800  # video gen is slow — generous 30 min ceiling
    node_output = None
    while time.time() < deadline:
        try:
            resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            resp.raise_for_status()
            history = resp.json()
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(5.0)
            continue
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            node_output = entry["outputs"].get(VIDEO_SAVE_NODE_ID, {})
            if node_output:
                break
            if status.get("status_str") == "error":
                raise RuntimeError(
                    f"Video job {prompt_id} errored: {status.get('messages')}")
            raise RuntimeError(f"Video job {prompt_id} completed but produced no output")
        time.sleep(5.0)
    else:
        raise TimeoutError(f"Video job {prompt_id} did not complete within 30 minutes")

    file_info = None
    for key in VIDEO_OUTPUT_KEYS:
        if node_output.get(key):
            file_info = node_output[key][0]
            break
    if file_info is None:
        raise RuntimeError(f"Video job {prompt_id} finished but no known output key matched: {list(node_output.keys())}")

    video_bytes = fetch_image_bytes(file_info)  # same /view mechanism works for any output file
    free_vram()
    return video_bytes


def generate_look_variants(variant_descs: list, photoreal: bool = True,
                           workflow_filename: str = "k2_txt2img_api.json") -> list:
    """For character-creation look candidates: render ONE portrait per entry in
    `variant_descs` (each an LLM-authored *different person* who still fits the
    user's concept). No imposed setup/framing — krea2's own workflow sets the
    background and composition (an injected studio framing caused a huge-head
    deformity on the tall 3:4 crop). Returns (image_bytes, filename) tuples.

    `photoreal=True` (default) prepends "photorealistic photograph of " so the
    workflow's TextGenerate node (which honors an explicit medium) stays
    photographic and doesn't occasionally drift into a cartoony render. Pass
    photoreal=False when the concept explicitly requests a cartoon/anime/stylized
    look, so that requested style is preserved.

    Efficiency: the UnloadAllModels node is skipped for all but the LAST
    candidate so the model stays warm across the look batch (no load time
    between candidates); the final candidate uses the normal unload to clear
    VRAM once the whole batch is done.
    """
    medium = "photorealistic photograph of " if photoreal else ""
    results = []
    errors = []
    last_idx = len(variant_descs) - 1
    for i, desc in enumerate(variant_descs):
        # Keep models warm between candidates; only the LAST one unloads VRAM.
        try:
            prompt_id = submit_workflow(
                medium + desc, batch_size=1, workflow_filename=workflow_filename,
                skip_unload=(i < last_idx),
                # Look candidates always stay portrait (3:4), independent of
                # /aspect cinematic mode; only final images/video go widescreen.
                apply_aspect=False,
                force_sfw=True,
            )
            info = wait_for_result(prompt_id)
            results.append((fetch_image_bytes(info), info["filename"]))
        except Exception as e:  # noqa: BLE001
            # One bad candidate must not discard the GPU work already done for
            # the others — deliver the survivors, report the failure inline.
            errors.append(f"candidate {i+1}: {type(e).__name__}: {e}")
            print(f"[comfy] look variant {i+1} failed: {e}")
            free_vram()  # candidate may have died mid-render; clear before next
    free_vram()
    if errors and not results:
        raise RuntimeError("all look candidates failed: " + "; ".join(errors))
    if errors:
        results.append(("__ERRORS__", " | ".join(errors)))
    return results


# Default voice descriptions per gender — used when the worker job doesn't
# supply a specific `voice_description` for the character.
# MOSS voice engine node graph (active voice workflow loaded below; sampling
# params are taken from the workflow file, not overridden here):
MOSS_VOICE_DESIGN_NODE = "7"
MOSS_SAVE_NODE = "11"
MOSS_WORKFLOW_FILE = "TTS_Suite_MOSS_Unload.json"


def generate_voice_candidates_moss(text: str, n: int = 3, gender_hint: str = "female",
                                   voice_description: str = "",
                                   workflow_filename: str = MOSS_WORKFLOW_FILE) -> list:
    """Generate `n` distinct natural voices via the MOSS engine (tts_audio_suite's
    UnifiedVoiceDesignerNode) on ComfyUI Desktop, port 8188. Varies `seed` per sample
    so each is a fresh, novel voice; sampling params come from the workflow file
    (TTS_Suite_MOSS_Unload.json), which also wires UnloadAllModels so MOSS is freed
    from VRAM after generation.

    Returns the same (audio_bytes, filename, subfolder, label) contract as the other
    voice-candidate generators so the VPS promote flow is unchanged. `text` (the
    sample line) maps to UnifiedVoiceDesignerNode's reference_text and
    `voice_description` (the character's tuning text) to voice_instruction, injected
    per candidate if provided."""
    import copy
    # NOTE: encoding="utf-8" is REQUIRED — this file contains a non-ASCII title
    # (⚙️ MOSS-TTS Engi…, byte 0x8f at position 787). Python's default text
    # open() on Windows uses the legacy charmap codec (cp1252), so reading it
    # raised UnicodeDecodeError and failed the whole voice_sample_batch job.
    with open(resolve_workflow_path(workflow_filename), encoding="utf-8") as f:
        base = json.load(f)
    results = []
    errors = []
    for i in range(n):
      try:
        wf = copy.deepcopy(base)
        vd = wf[MOSS_VOICE_DESIGN_NODE]["inputs"]
        # Sample line and voice persona map 1:1 to UnifiedVoiceDesignerNode's
        # reference_text / voice_instruction (same fields the old graph used for text
        # / instruction). Sampling params are the workflow's own.
        if voice_description:
            vd["voice_instruction"] = voice_description
        vd["reference_text"] = text
        vd["seed"] = random.randint(0, 2 ** 63 - 1)
        wf[MOSS_SAVE_NODE]["inputs"]["filename_prefix"] = f"audio/moss_{i+1}_{random.randint(0,99999)}"

        prompt_id = submit_prompt(wf)

        deadline = time.time() + 300
        audio = None
        while time.time() < deadline:
            try:
                resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
                resp.raise_for_status()
                history = resp.json()
            except (requests.exceptions.RequestException, ValueError):
                time.sleep(2.0)
                continue
            if prompt_id in history:
                save = history[prompt_id]["outputs"].get(MOSS_SAVE_NODE, {})
                audio = save.get("audio", [])
                if audio:
                    break
                if history[prompt_id].get("status", {}).get("status_str") == "error":
                    raise RuntimeError(f"MOSS job {prompt_id} errored: "
                       f"{history[prompt_id]['status'].get('messages')}")
            time.sleep(2.0)
        if not audio:
            raise TimeoutError(f"MOSS job {prompt_id} did not complete within 300s")

        audio_bytes = fetch_image_bytes(audio[0])
        results.append((audio_bytes, audio[0]["filename"], audio[0].get("subfolder", ""),
                        f"moss-{i+1}"))
      except Exception as e:  # noqa: BLE001 - deliver surviving candidates
        errors.append(f"candidate {i+1}: {type(e).__name__}: {e}")
        print(f"[comfy] MOSS candidate {i+1} failed: {e}")
    free_vram()
    if errors and not results:
        raise RuntimeError("all MOSS voice candidates failed: " + "; ".join(errors))
    if errors:
        results.append((b"__ERRORS__", "", "", " | ".join(errors)))
    return results

def promote_selected_voice(filename: str, subfolder: str = "") -> str:
    """Store a trimmed copy of the selected recording, without a synthesis job."""
    from worker.voice_trim import trim_candidate
    response = requests.get(
        f"{COMFY_URL}/view",
        params={"filename": filename, "subfolder": subfolder, "type": "output"}, timeout=30,
    )
    response.raise_for_status()
    trimmed = trim_candidate(response.content)
    return upload_bytes_to_input(trimmed, f"voice_reference_{uuid.uuid4().hex}.wav")


def generate_moss_line_from_saved_voice(voice_audio: str, text: str) -> str:
    """Synthesize exact text; reuse design metadata or clone the saved audio."""
    target = os.path.basename(voice_audio or "")
    if not target or not text.strip():
        raise ValueError("saved voice and requested speech are required")
    response = requests.get(f"{COMFY_URL}/history", timeout=30)
    response.raise_for_status()
    source_inputs = None
    for entry in reversed(list(response.json().values())):
        outputs = entry.get("outputs", {}).get(MOSS_SAVE_NODE, {}).get("audio", [])
        if not any(os.path.basename(item.get("filename", "")) == target for item in outputs):
            continue
        prompt = entry.get("prompt") or []
        graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
        candidate = graph.get(MOSS_VOICE_DESIGN_NODE, {}).get("inputs", {})
        if isinstance(candidate.get("seed"), int) and candidate.get("voice_instruction"):
            source_inputs = candidate
            break
    with open(resolve_workflow_path(MOSS_WORKFLOW_FILE), encoding="utf-8") as f:
        workflow = copy.deepcopy(json.load(f))
    if source_inputs is not None:
        designer = workflow[MOSS_VOICE_DESIGN_NODE]["inputs"]
        designer["reference_text"] = text.strip()
        designer["seed"] = source_inputs["seed"]
        designer["voice_instruction"] = source_inputs["voice_instruction"]
    else:
        # ComfyUI history is volatile. Old voices must still work after restart.
        # The installed MOSS speech model clones the actual saved voice sample.
        workflow["6"]["inputs"]["model_variant"] = "1.7B"
        workflow["12"] = {"class_type": "LoadAudio", "inputs": {"audio": voice_audio}}
        workflow[MOSS_VOICE_DESIGN_NODE] = {
            "class_type": "UnifiedTTSTextNode",
            "inputs": {"TTS_engine": ["6", 0], "text": text.strip(),
                       "narrator_voice": "none", "seed": 1,
                       "opt_narrator": ["12", 0], "enable_chunking": False},
        }
        workflow["10"]["inputs"]["value"] = [MOSS_VOICE_DESIGN_NODE, 0]
    workflow[MOSS_SAVE_NODE]["inputs"]["filename_prefix"] = (
        f"audio/continuation_line_{random.randint(0, 99999999):08d}"
    )
    prompt_id = submit_prompt(workflow)
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            poll = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            poll.raise_for_status()
            entry = poll.json().get(prompt_id)
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(2.0)
            continue
        if entry:
            audio = entry.get("outputs", {}).get(MOSS_SAVE_NODE, {}).get("audio", [])
            if audio:
                info = audio[0]
                promoted = promote_output_to_input(
                    info["filename"], subfolder=info.get("subfolder", "")
                )
                free_vram()
                return promoted
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError(
                    f"MOSS requested-line job {prompt_id} errored: "
                    f"{entry.get('status', {}).get('messages')}"
                )
        time.sleep(2.0)
    raise TimeoutError(f"MOSS requested-line job {prompt_id} did not complete within 300s")


def _apply_h3_speech_guide(workflow, audio_filename, duration_seconds):
    """Anchor exact speech followed by silence in initial and continuation clips."""
    if not audio_filename:
        return
    if "147" not in workflow:
        workflow["147"] = {
            "class_type": "MiniMaxH3AddGuide",
            "inputs": {"positive": ["136", 0], "latent": ["136", 1],
                       "frame_idx": 0, "audio_vae": ["120", 0]},
        }
    workflow["148"] = {"class_type": "LoadAudio", "inputs": {"audio": audio_filename}}
    # Cover rounded video frames too; the guide crops excess audio.
    workflow["149"] = {"class_type": "EmptyAudio", "inputs": {
        "duration": duration_seconds + 1.0, "sample_rate": 44100, "channels": 2}}
    workflow["150"] = {"class_type": "AudioConcat", "inputs": {
        "audio1": ["148", 0], "audio2": ["149", 0], "direction": "after"}}
    workflow["147"]["inputs"]["audio"] = ["150", 0]
    workflow["126"]["inputs"]["conditioning"] = ["147", 0]


def generate_voice_reference_video(reference_image: str, voice_audio: str,
                                   h3_prompt: str,
                                   duration_seconds: float = 5.0,
                                   workflow_filename: str = "presets/minimax_h3_ref2v_vertical.json",
                                   guided_speech_audio: str = None) -> bytes:
    """MiniMax H3 Reference-to-Video: animates the identity-locked `reference_image`
    while `<Audio 1>` (the character's voice sample, in /input) makes the character
    SPEAK — not narrate over the scene.

    `h3_prompt` is an LLM-rewritten, properly-formatted H3 I2VA body (the three core
    sections) that already references <Picture 1> and <Audio 1> and contains the
    character's dialogue. We prepend the official I2VA instruction line so the model
    binds <Picture 1> as the frame anchored at 0.00s. The prompt MUST include the
    `<Audio 1>` conditioning tag (H3 uses these tags to bind references); without it
    H3 treats the audio as ambient narration."""
    # Prepend the official I2VA reference-instruction line (H3 prompt-writing guide).
    prompt_text = (
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        + h3_prompt.strip()
    )
    with open(resolve_workflow_path(workflow_filename), encoding="utf-8") as f:
        workflow = json.load(f)
    workflow = copy.deepcopy(workflow)

    workflow[REF2V_IMAGE_NODE_ID]["inputs"]["image"] = reference_image
    workflow[REF2V_AUDIO_NODE_ID]["inputs"]["audio"] = voice_audio
    _apply_h3_speech_guide(workflow, guided_speech_audio, duration_seconds)
    workflow[REF2V_PROMPT_NODE_ID]["inputs"]["value"] = prompt_text
    workflow[REF2V_DURATION_NODE_ID]["inputs"]["value"] = duration_seconds
    workflow[REF2V_SEED_NODE_ID]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)
    # Reels-style vertical (9:16) by default; widescreen when /aspect is cinematic.
    wf_aspect, wf_mp = _video_resolution()
    workflow[REF2V_RES_NODE_ID]["inputs"]["aspect_ratio"] = wf_aspect
    workflow[REF2V_RES_NODE_ID]["inputs"]["megapixels"] = wf_mp

    prompt_id = submit_prompt(workflow)

    deadline = time.time() + 1800  # video gen is slow — generous 30 min ceiling
    while time.time() < deadline:
        try:
            resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            resp.raise_for_status()
            history = resp.json()
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(5.0)
            continue
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            outputs = entry["outputs"].get(REF2V_SAVE_NODE_ID, {})
            file_info = None
            for key in ("videos", "gifs", "images"):
                if outputs.get(key):
                    file_info = outputs[key][0]
                    break
            if file_info is None:
                if status.get("status_str") == "error":
                    raise RuntimeError(
                        f"Ref2V job {prompt_id} errored: {status.get('messages')}")
                raise RuntimeError(f"Ref2V job {prompt_id} finished but no output file matched")
            video_bytes = fetch_image_bytes(file_info)
            free_vram()
            return video_bytes
        time.sleep(5.0)
    raise TimeoutError(f"Ref2V job {prompt_id} did not complete within 30 minutes")


def generate_multichar_voice_reference_video(
    ref_image_a: str, ref_image_b: str, scene_image: str,
    voice_audio_a: str, voice_audio_b: str,
    h3_prompt: str,
    duration_seconds: float = 5.0,
    seed: int = None,
    workflow_filename: str = None,
    use_audio_references: bool = True,
    audio_reference_speakers: list = None,
    performance_plan: dict = None,
) -> bytes:
    """MiniMax H3 two-character Reference-to-Video: animates two identity-
    locked characters in the same frame, each with their own voice reference.

    `ref_image_a/b`: character identity images (in ComfyUI /input).
    `scene_image`: the composited scene/composition image.
    `voice_audio_a/b`: character voice samples (in ComfyUI /input).
    `h3_prompt`: LLM-rewritten 2-char H3 body (see build_multichar_h3_video_prompt).
    `workflow_filename`: if None/empty, auto-selects via _NEWDEFAULTS_2CHAR_H3
    by current aspect mode.

    Sets <Picture 1> = char A, <Picture 2> = char B, <Picture 3> = scene,
    <Audio 1> = char A, <Audio 2> = char B.
    """
    if not workflow_filename:
        workflow_filename = _NEWDEFAULTS_2CHAR_H3.get(
            _aspect_mode(),
            "presets/minimax_h3_2char_ref2v_widescreen.json"
        )

    # This workflow uses H3 full-reference mode. The complete six-section prompt
    # is compiled upstream; the single-character I2VA first-frame prefix does not
    # apply here.
    prompt_text = h3_prompt.strip()
    print(f"[comfy_client] 2-char H3 final prompt (first 600): {prompt_text[:600]}")

    with open(resolve_workflow_path(workflow_filename), encoding="utf-8") as f:
        workflow = json.load(f)
    workflow = copy.deepcopy(workflow)

    required_nodes = {"136", REF2V_IMAGE_NODE_ID, TWOCHAR_REF_IMAGE_B, TWOCHAR_REF_SCENE,
                      REF2V_PROMPT_NODE_ID, REF2V_DURATION_NODE_ID, REF2V_SEED_NODE_ID, REF2V_RES_NODE_ID}
    if use_audio_references:
        required_nodes.update((REF2V_AUDIO_NODE_ID, TWOCHAR_REF_AUDIO_B))
    missing = [node for node in sorted(required_nodes)
               if node not in workflow or not isinstance(workflow[node].get("inputs"), dict)]
    if missing:
        raise ValueError("H3 workflow missing required reference nodes: " + ", ".join(missing))

    # Identity images
    workflow[REF2V_IMAGE_NODE_ID]["inputs"]["image"] = ref_image_a
    workflow[TWOCHAR_REF_IMAGE_B]["inputs"]["image"] = ref_image_b
    # Scene/composition
    workflow[TWOCHAR_REF_SCENE]["inputs"]["image"] = scene_image
    # Silent continuations must not condition on recordings that contain words.
    # Leaving those references connected causes H3 to invent pseudo-speech even
    # when the prompt explicitly requests silence.
    speakers = (["a", "b"] if audio_reference_speakers is None
                else list(dict.fromkeys(audio_reference_speakers)))
    if performance_plan is not None:
        from worker.performance_plan import validate_compiled
        validate_compiled(prompt_text, performance_plan, speakers if use_audio_references else [], duration_seconds)
        if use_audio_references != bool(performance_plan["turns"]):
            raise ValueError("Audio conditioning conflicts with the validated performance plan")
    if any(key not in ("a", "b") for key in speakers):
        raise ValueError("Unknown audio-reference speaker")
    if use_audio_references and speakers:
        voice_by_speaker = {"a": voice_audio_a, "b": voice_audio_b}
        selected = [(key, voice_by_speaker[key]) for key in speakers
                    if key in voice_by_speaker]
        audio_nodes = [REF2V_AUDIO_NODE_ID, TWOCHAR_REF_AUDIO_B]
        for index, (_, filename) in enumerate(selected):
            workflow[audio_nodes[index]]["inputs"]["audio"] = filename
            workflow["136"]["inputs"][f"ref_audios.ref_audio_{index}"] = [audio_nodes[index], 0]
        for index in range(len(selected), 2):
            workflow["136"]["inputs"].pop(f"ref_audios.ref_audio_{index}", None)
            workflow.pop(audio_nodes[index], None)
    else:
        workflow["136"]["inputs"].pop("ref_audios.ref_audio_0", None)
        workflow["136"]["inputs"].pop("ref_audios.ref_audio_1", None)
        workflow.pop(REF2V_AUDIO_NODE_ID, None)
        workflow.pop(TWOCHAR_REF_AUDIO_B, None)
    # Prompt, duration, seed
    workflow[REF2V_PROMPT_NODE_ID]["inputs"]["value"] = prompt_text
    workflow[REF2V_DURATION_NODE_ID]["inputs"]["value"] = duration_seconds
    workflow[REF2V_SEED_NODE_ID]["inputs"]["noise_seed"] = (
        random.randint(0, 2**31 - 1) if seed is None else int(seed)
    )
    # Resolution
    wf_aspect, wf_mp = _video_resolution()
    workflow[REF2V_RES_NODE_ID]["inputs"]["aspect_ratio"] = wf_aspect
    workflow[REF2V_RES_NODE_ID]["inputs"]["megapixels"] = wf_mp

    if performance_plan is not None:
        # Validate the actual graph after wiring: identity, scene, voice order,
        # prompt and duration must agree with the compiled contract.
        inputs = workflow["136"]["inputs"]
        for index, node in enumerate((REF2V_IMAGE_NODE_ID, TWOCHAR_REF_IMAGE_B, TWOCHAR_REF_SCENE)):
            if inputs.get(f"ref_images.ref_image_{index}") != [node, 0]:
                raise ValueError("H3 workflow identity/scene reference mapping changed")
        audio_nodes = [REF2V_AUDIO_NODE_ID, TWOCHAR_REF_AUDIO_B]
        voices = {"a": voice_audio_a, "b": voice_audio_b}
        for index in range(2):
            binding = inputs.get(f"ref_audios.ref_audio_{index}")
            if index < len(speakers) and use_audio_references:
                if binding != [audio_nodes[index], 0] or workflow[audio_nodes[index]]["inputs"].get("audio") != voices[speakers[index]]:
                    raise ValueError("H3 workflow audio mapping changed")
            elif binding is not None:
                raise ValueError("H3 workflow contains unintended audio conditioning")
        validate_compiled(workflow[REF2V_PROMPT_NODE_ID]["inputs"]["value"], performance_plan,
                          speakers if use_audio_references else [], workflow[REF2V_DURATION_NODE_ID]["inputs"]["value"])

    prompt_id = submit_prompt(workflow)

    deadline = time.time() + 3600  # 1 hour — 2-char H3 can be slow
    while time.time() < deadline:
        try:
            resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            resp.raise_for_status()
            history = resp.json()
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(5.0)
            continue
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            outputs = entry["outputs"].get(REF2V_SAVE_NODE_ID, {})
            file_info = None
            for key in ("videos", "gifs", "images"):
                if outputs.get(key):
                    file_info = outputs[key][0]
                    break
            if file_info is None:
                if status.get("status_str") == "error":
                    raise RuntimeError(
                        f"2-char H3 job {prompt_id} errored: {status.get('messages')}")
                raise RuntimeError(f"2-char H3 job {prompt_id} finished but no output file matched")
            video_bytes = fetch_image_bytes(file_info)
            free_vram()
            return video_bytes
        time.sleep(5.0)
    raise TimeoutError(f"2-char H3 job {prompt_id} did not complete within 1 hour")
