"""
Worker side of the minimal slice, runs on the home GPU box.

Loop: poll the VPS for the next queued job, drive Ollama (text/LLM phases)
and ComfyUI (images / video / MOSS voice via comfy_client), post results back.

Env vars:
  VPS_URL      - e.g. http://100.x.y.z:8787  (VPS's Tailscale IP)
  OLLAMA_URL   - defaults to http://127.0.0.1:11434
  OLLAMA_MODEL - defaults to qwen3-14b-64k
  POLL_SECONDS - defaults to 2
"""

import os
import sys
import time
import base64
import json
import re
import random
import shutil
import subprocess
import requests
import socket
from datetime import datetime

# Heartbeat file helpers -----------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_client
from scripts.runtime import Heartbeat, InstanceLock, resource_identity, LOG_DIR
from worker.image_prompts import build_image_prompt, build_expression_instruction, assemble_final_prompt
from worker.job_runtime import JobClient, CompletionPending, LeaseLost
from vps.artifacts import resolve_artifact
from worker.video_contract import VideoRequest, speech_spans, dialogue_candidates, normalize_quotes, non_speech_text, has_unresolved_speech
from worker import performance_plan


def extract_video_continuation_frame(relative_path, offset_seconds=0.15):
    """Return a PNG just before the source clip ends, without platform shell syntax."""
    source = resolve_artifact(relative_path)
    if not source.is_file():
        raise FileNotFoundError("Continuation source video is missing")
    configured = os.environ.get("FFMPEG_PATH", "ffmpeg")
    executable = shutil.which(configured) if not os.path.isabs(configured) else configured
    if not executable or not os.path.isfile(executable):
        raise RuntimeError("Video continuation requires FFmpeg on PATH or FFMPEG_PATH")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    detail = ""
    for offset in (max(0.05, float(offset_seconds)), 0.5, 1.0):
        command = [executable, "-hide_banner", "-loglevel", "error", "-sseof",
                   f"-{offset:.3f}", "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
                   "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   check=False, timeout=60, creationflags=flags)
        if completed.returncode == 0 and completed.stdout.startswith(b"\x89PNG"):
            return completed.stdout
        detail = completed.stderr.decode("utf-8", "replace")[-500:]
    raise RuntimeError(f"Could not extract a continuation frame: {detail or 'FFmpeg returned no image'}")


def continuation_scene_instruction(requested):
    return (
        "Start from the exact visible state of the supplied final frame. Preserve identity and "
        "unspecified setting, clothing, props and camera details. Advance only the requested changes; "
        "do not replay completed actions. New authored timeline: " + requested
    )


def build_h3_continuation_prompt(motion_raw: str, character_name: str,
                                 spoken_line: str = None,
                                 has_audio: bool = True) -> str:
    return VideoRequest.create(motion_raw, character_name, spoken_line).compile(
        has_audio=has_audio, continuation=True)


def touch_heartbeat(name):
    # Compatibility for existing loop calls. Liveness is maintained independently.
    pass
# Per-call timeout is handled by requests' own timeout= kwarg; the old
# socket.setdefaulttimeout(30) silently killed long POSTs by overriding
# per-request timeouts at a lower level. Let requests handle all timeouts.
import comfy_client

VPS_URL = os.environ["VPS_URL"].rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-14b-64k")
# Per-request ceiling for Ollama chat calls. 300s proved too tight: big/slow
# models (MoE switches, long roleplay-history prompts) occasionally exceed 5 min
# mid-generation (job 625 died here: ReadTimeout during the H3 rewrite), killing
# the whole video job before rendering started. 15 min covers the observed worst
# cases; override via env if needed.
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "120"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))


def video_start_delay_seconds() -> float:
    """Return the non-negative image-to-video handoff pause (default 5s)."""
    try:
        return max(0.0, float(os.environ.get("VIDEO_START_DELAY_SECONDS", "5")))
    except ValueError:
        print("[worker] invalid VIDEO_START_DELAY_SECONDS; using 5s", flush=True)
        return 5.0


_active_settings = {}


def get_current_model() -> str:
    if _active_settings.get("model"):
        return _active_settings["model"]
    """Checks the VPS for a model selected via /model; falls back to the
    OLLAMA_MODEL env var default if nothing's been picked yet."""
    try:
        resp = requests.get(f"{VPS_URL}/settings/model", timeout=10)
        resp.raise_for_status()
        return resp.json().get("model") or OLLAMA_MODEL
    except (requests.exceptions.RequestException, ValueError):
        # ValueError: non-JSON VPS response (e.g. an HTML error page) must not
        # escape — an escaping exception here used to kill the whole loop.
        return OLLAMA_MODEL


def get_current_workflow(step_key: str) -> str:
    if step_key in _active_settings.get("workflows", {}):
        return _active_settings["workflows"][step_key]
    """Checks the VPS for the workflow selected via /workflow for a generation
    step; falls back to the VPS-side default strategy key if unavailable."""
    try:
        resp = requests.get(f"{VPS_URL}/settings/workflow/{step_key}", timeout=10)
        resp.raise_for_status()
        return resp.json().get("workflow")
    except (requests.exceptions.RequestException, ValueError):
        return None


# Module-global so tests/env can force a mode without hitting the VPS.
_content_mode_override = None
# Cache the fetched mode for 5s so the per-prompt builders (enhance_scene,
# chat and planning) don't hit the VPS with an HTTP call on every prompt,
# while a /mode change still propagates within ~5s on the running worker.
_mode_cache = {"t": 0.0, "v": "sfw"}

# Generate unload flow disabled — unused node (no Krea2 cache-freed node wired).
_MINIMAX_UNLOAD_NODE = None

# Default scene-image workflows for the content×aspect matrix. Used by both
# character_image_edit and character_video scene-image steps.
_NEWDEFAULTS_REF = comfy_client._NEWDEFAULTS_2CHAR_REF


def get_content_mode() -> str:
    if "content_mode" in _active_settings:
        return _active_settings["content_mode"]
    """Return the global content mode ('nsfw' | 'sfw') chosen via /mode, cached.

    Precedence: explicit _content_mode_override (test) > the VPS /mode value
    (the live /mode switch) > env CONTENT_MODE (test-only fallback) > 'sfw'.
    The env var must NOT shadow the live switch, or a stale CONTENT_MODE env
    (e.g. from .env) silently pins the worker to nsfw and /mode has no effect.
    """
    global _content_mode_override
    if _content_mode_override:
        return _content_mode_override
    now = time.time()
    if now - _mode_cache["t"] < 5:
        return _mode_cache["v"]
    try:
        resp = requests.get(f"{VPS_URL}/settings/content_mode", timeout=10)
        resp.raise_for_status()
        v = resp.json().get("mode", "sfw") or "sfw"
        v = v if v in ("sfw", "nsfw") else "sfw"
        _mode_cache.update(t=now, v=v)
        return v
    except (requests.exceptions.RequestException, ValueError):
        # VPS unreachable: fall back to the test env force, else the cache.
        forced = os.environ.get("CONTENT_MODE")
        if forced in ("sfw", "nsfw"):
            return forced
        return _mode_cache["v"]


# Global aspect mode ('vertical' | 'cinematic'), set via /aspect and cached the
# same way as content mode so a toggle propagates to the running worker in ~5s.
_aspect_mode_override = None
_aspect_cache = {"t": 0.0, "v": "vertical"}


def get_aspect_mode() -> str:
    if "aspect_mode" in _active_settings:
        return _active_settings["aspect_mode"]
    """Return the global aspect mode ('vertical' | 'cinematic') via /aspect, cached.

    Precedence: explicit _aspect_mode_override (test) > the VPS /aspect value
    (the live /aspect switch) > env ASPECT_MODE (test-only fallback) > 'vertical'.
    The env var must NOT shadow the live switch (same rule as content mode).
    """
    global _aspect_mode_override
    if _aspect_mode_override:
        return _aspect_mode_override
    now = time.time()
    if now - _aspect_cache["t"] < 5:
        return _aspect_cache["v"]
    try:
        resp = requests.get(f"{VPS_URL}/settings/aspect_mode", timeout=10)
        resp.raise_for_status()
        v = resp.json().get("mode", "vertical") or "vertical"
        v = v if v in ("cinematic", "vertical") else "vertical"
        _aspect_cache.update(t=now, v=v)
        return v
    except (requests.exceptions.RequestException, ValueError):
        forced = os.environ.get("ASPECT_MODE")
        if forced in ("cinematic", "vertical"):
            return forced
        return _aspect_cache["v"]


# Character-initiated video toggle (set via /videos), cached like content mode
# so a toggle propagates to the running worker within ~5s.
_videos_cache = {"t": 0.0, "v": True}


def get_character_videos_enabled() -> bool:
    if "character_videos" in _active_settings:
        return _active_settings["character_videos"]
    """Return whether characters may INITIATE sending videos in chat.

    Reads /settings/character_videos from the VPS with a 5s cache. Defaults to
    True when unset or when the VPS is unreachable (fail-open, matching the
    historical behavior). Only gates the character's own medium choice — an
    explicit /video <desc> job enqueues as character_video directly and never
    consults this."""
    global _videos_cache
    now = time.time()
    if now - _videos_cache["t"] < 5:
        return _videos_cache["v"]
    try:
        resp = requests.get(f"{VPS_URL}/settings/character_videos", timeout=10)
        resp.raise_for_status()
        v = resp.json().get("enabled", True)
        v = bool(v)
        _videos_cache.update(t=now, v=v)
        return v
    except (requests.exceptions.RequestException, ValueError):
        # VPS unreachable: keep last known value (default on).
        return _videos_cache["v"]


def get_current_ollama_url() -> str:
    """Fetch the Ollama base URL chosen via /endpoints from the VPS; falls back
    to the OLLAMA_URL env default. Mirrors get_current_model and lets a runtime
    /endpoints change take effect on the running worker without a restart."""
    try:
        resp = requests.get(f"{VPS_URL}/settings/endpoints", timeout=10)
        resp.raise_for_status()
        return resp.json().get("ollama") or OLLAMA_URL
    except (requests.exceptions.RequestException, ValueError):
        return OLLAMA_URL


def get_current_comfyui_url() -> str:
    """Fetch the ComfyUI base URL chosen via /endpoints from the VPS. Falls back
    to whatever comfy_client already resolved. To actually TAKE EFFECT on the
    running worker, callers set comfy_client.COMFY_URL to the returned value."""
    try:
        resp = requests.get(f"{VPS_URL}/settings/endpoints", timeout=10)
        resp.raise_for_status()
        return resp.json().get("comfyui") or comfy_client.COMFY_URL
    except (requests.exceptions.RequestException, ValueError):
        return comfy_client.COMFY_URL


def build_profile_prompt(personality_notes: str) -> str:
    return (
        "Write a concise 2-4 sentence system prompt for an AI character "
        f"with this personality: {personality_notes}\n"
        "Output only the system prompt text, no preamble or explanation."
    )


def _strip_thinking(content: str) -> str:
    """Qwen3's thinking mode sometimes leaks its reasoning trace into
    message.content (Ollama only strips <thinking>-tagged blocks, not plain
    reasoning prose). The trace is always followed by a standalone 'response'
    marker line, then the real answer. Drop everything up to and including the
    LAST standalone response marker. 'response' is matched as its own word so we
    never cut a real answer that merely contains the word 'response'."""
    import re
    # marker = 'response' as a whole word, with optional surrounding whitespace,
    # followed by a newline (the answer begins on the next line).
    m = list(re.finditer(r"(?i)(?:^|\s)response\s*\n", content))
    if m:
        content = content[m[-1].end():]
    return content.strip()


def evict_current_model(model: str = None):
    """Unload the selected model and require HTTP confirmation before GPU handoff."""
    selected = model or get_current_model()
    response = requests.post(f"{get_current_ollama_url()}/api/generate",
        json={"model": selected, "prompt": "", "keep_alive": 0}, timeout=30)
    response.raise_for_status()
    deadline = time.monotonic() + 30
    expected = selected if ":" in selected else selected + ":latest"
    while True:
        status = requests.get(f"{get_current_ollama_url()}/api/ps", timeout=10)
        status.raise_for_status()
        names = {item.get("name", item.get("model", "")) for item in status.json().get("models", [])}
        if selected not in names and expected not in names:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Model remained resident after unload request; media handoff stopped")
        time.sleep(0.5)
    print(f"[ollama] evicted {selected}")


def call_ollama(prompt: str, json_mode: bool = False, model: str = None,
               keep_alive: str = None, messages: list = None) -> str:
    body = {
        "model": model or get_current_model(),
        "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
        "stream": False,
        # Small context window on purpose: our prompts are <=~600 tokens, but
        # OLLAMA_CONTEXT_LENGTH (registry env) is 65536 — clamped to the model's
        # train limit. KV cache scales with ctx, and an oversized cache forces
        # llama.cpp to spill weights to CPU ("offloaded 27/41 layers"), dropping
        # decode to ~4 tok/s (job-627 measurement). 8192 keeps the whole KV
        # cache on-GPU so more/fewer-exchanged layers fit in VRAM.
        "options": {"num_ctx": 8192},
        # no keep_alive here on purpose: omit it so Ollama uses the server-wide
        # OLLAMA_KEEP_ALIVE setting (which the operator controls) instead of
        # pinning the model resident for 5m locally. keep VRAM free for ComfyUI.
        # Disable Qwen3 reasoning/thinking mode. It caused BOTH a leaked
        # chain-of-thought in the output AND pathological slowness (the prompt
        # rewrite sometimes exceeded a 5-min timeout just regurgitating its
        # deliberations). Direct answers are fast and clean.
        "think": False,
    }
    if keep_alive:
        # Pin the model resident only when the caller explicitly asks (job LLM phase),
        # so consecutive calls share one loaded model instead of reloading between them.
        body["keep_alive"] = keep_alive
    if json_mode:
        # Ollama's grammar-constrained JSON mode — guarantees syntactically valid
        # JSON (proper double quotes etc.), rather than just asking nicely in the
        # prompt and hoping the model follows it.
        body["format"] = json_mode if isinstance(json_mode, dict) else "json"

    resp = requests.post(f"{get_current_ollama_url()}/api/chat", json=body, timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]

    # Qwen3's thinking mode wraps a reasoning trace before the real answer,
    # sometimes with a stray leading artifact. Drop any leaked thinking trace.
    return _strip_thinking(content)


def enforce_movement_fidelity(source: str, rewritten: str) -> str:
    """Compatibility hook: only the original timeline owns actions and direction."""
    return (source or "").strip()


def build_h3_video_prompt(motion_raw: str, character_name: str,
                          spoken_line: str = None, has_audio: bool = True,
                          keep_alive: str = None, scene_text: str = None) -> str:
    """Compile the original request, never a free-form visual expansion.

    Code owns exact audio and the ordered source timeline, as in the multi-person
    compiler. keep_alive remains accepted for existing callers; no model is loaded.
    """
    return VideoRequest.create(motion_raw, character_name, spoken_line).compile(has_audio)


def build_multichar_h3_video_prompt(char_a_name: str, char_a_concept: str,
                                     char_b_name: str, char_b_concept: str,
                                     scene_text: str, interaction_text: str,
                                     spoken_line_a: str = None,
                                     spoken_line_b: str = None,
                                     dialogue_order=None,
                                     dialogue_turns=None,
                                     scene_plan=None,
                                     keep_alive: str = None) -> str:
    """Compile a two-character scene into an H3 full-reference prompt.

    The normal job path supplies a validated scene plan produced before image
    generation. Legacy callers also pass through deterministic compilation
    and the same validation gate.

    Subject and picture numbers follow character input order. Speaker IDs and
    audio numbers independently follow first vocal appearance, per H3's guide.
    """
    expected_lines = {"a": spoken_line_a, "b": spoken_line_b}
    order = [key for key in (dialogue_order or ["a", "b"]) if expected_lines.get(key)]
    exact_turns = dialogue_turns if dialogue_turns is not None else [
        {"speaker": key, "line": expected_lines[key]} for key in order
    ]
    return _deterministic_multichar_h3_prompt(
        char_a_name, char_a_concept, char_b_name, char_b_concept,
        scene_text, expected_lines, order, dialogue_turns=exact_turns,
        scene_plan=scene_plan,
    )


def _multichar_dialogue_turns_are_exact(body, dialogue_turns):
    blocks = []
    for match in re.finditer(r"<d[^>]*>(.*?)</d>", body or "", re.I | re.S):
        words = re.sub(r"^\s*\[English\]\s*", "", match.group(1), flags=re.I).strip()
        prefix = (body or "")[max(0, match.start() - 220):match.start()]
        audio = re.findall(r"<Audio\s+([12])>", prefix, re.I)
        blocks.append((words, audio[-1] if audio else None))
    speaking_keys = list(dict.fromkeys(turn["speaker"] for turn in dialogue_turns))
    audio_ids = {key: str(index + 1) for index, key in enumerate(speaking_keys)}
    expected = [(turn["line"], audio_ids[turn["speaker"]]) for turn in dialogue_turns]
    return blocks == expected


def _multichar_dialogue_is_exact(body, lines, order):
    turns = [{"speaker": key, "line": lines[key]} for key in order]
    return _multichar_dialogue_turns_are_exact(body, turns)


_MULTICHAR_SPEECH = (
    r"(?:says?|saying|said|asks?|asking|asked|replies|reply|replied|responds?|responded|"
    r"tells?|told|speaks?|speaking|spoke|whispers?|whispering|whispered|shouts?|shouting|shouted|"
    r"purrs?|purred|murmurs?|murmured)"
)
_MULTICHAR_VOCAL = re.compile(
    rf"\b{_MULTICHAR_SPEECH}\b|"
    r"\b(?:speaking|talking|laughter|laughing|laughs?|giggles?|giggling|"
    r"panting|moans?|moaning|groans?|groaning|gasps?|gasping|sighs?|sighing|"
    r"singing|sings?|chanting|chatter|voices?|speech|vocalizations?)\b",
    re.I,
)


def _require_multichar_scene_plan(plan, turns):
    """All producers, including fallbacks, must pass the same contract."""
    if not _validate_multichar_scene_plan(plan, turns):
        raise ValueError(
            "Video plan contains unresolved speech cues or inconsistent dialogue/actions. "
            "Use explicitly named speakers and put every spoken line in quotes."
        )
    return plan


def _multichar_name_aliases(char_a_name, char_b_name):
    names = [char_a_name, char_b_name]
    candidates = []
    for name in names:
        parts = name.split()
        first = parts[1] if len(parts) > 1 and parts[0].lower() in ("captain", "commander", "doctor", "dr", "sir") else parts[0]
        candidates.append(first)
    return {alias: name for alias, name in zip(candidates, names) if candidates.count(alias) == 1}


def _expand_multichar_names(scene_text, char_a_name, char_b_name):
    """Normalize unambiguous short attributions without touching quoted words."""
    aliases = _multichar_name_aliases(char_a_name, char_b_name)
    full = sorted((char_a_name, char_b_name), key=len, reverse=True)
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in full + list(aliases)) + r")\b", re.I)
    mapping = {name.casefold(): name for name in full}
    mapping.update({alias.casefold(): name for alias, name in aliases.items()})
    chunks = re.split(r'("[^"\n]*")', normalize_multichar_dialogue_quotes(scene_text))
    return "".join(chunk if index % 2 else pattern.sub(lambda m: mapping[m.group().casefold()], chunk)
                   for index, chunk in enumerate(chunks))


def _multichar_visual_scene(scene_text, char_a_name, char_b_name):
    """Remove quoted speech and its attribution while retaining visual actions."""
    # First names are common in authored dialogue even when cast records use
    # full names. Only accept a short name when it identifies one cast member.
    names = [char_a_name, char_b_name]
    aliases = names + list(_multichar_name_aliases(char_a_name, char_b_name))
    speaker = rf"(?:{'|'.join(re.escape(name) for name in sorted(set(aliases), key=len, reverse=True))}|he|she|they)"
    scene_text = normalize_multichar_dialogue_quotes(scene_text)
    speech = _MULTICHAR_SPEECH
    quote = r'["“][^"”\n]*["”]'
    def remove_speech(match):
        # Keep the actor when the same clause continues with a visible action.
        tail = (scene_text or "")[match.end():]
        if re.match(r"\s+and\s+(?:walks?|opens?|closes?|turns?|smiles?|sits?|stands?|holds?|looks?|steps?|winks?)\b", tail, re.I):
            return match.group("speaker")
        return ""

    # A requested nonverbal cue immediately attached to authored dialogue is
    # compiled into a silent visible action by _multichar_performance_hints.
    # Remove the original vocal wording here so it does not survive as an
    # unbounded sound cue and trip the anti-gibberish contract.
    attached_vocal = r"(?:gasps?|sighs?|laughs?|giggles?|moans?|groans?)"
    scene = re.sub(
        rf"\b(?:{speaker})\s+{attached_vocal}\s+and\s+{speech}\b"
        rf"[^.?!\n\"“]{{0,100}}(?:{quote})",
        "", scene_text or "", flags=re.I,
    )
    # Handle dialogue written before its attribution: '"Wait," says Alex'.
    scene = re.sub(
        rf"{quote}\s*,?\s*{speech}\s+(?:{speaker})\b",
        "", scene, flags=re.I,
    )
    scene = re.sub(rf"\b{speaker}\s*:\s*{quote}", "", scene, flags=re.I)
    scene = re.sub(
        rf"\b(?P<speaker>{speaker})\s+(?:[A-Za-z]+ly\s+)?{speech}\b[^.?!\n\"“]{{0,100}}(?:{quote})",
        remove_speech, scene, flags=re.I,
    )
    # Remove attribution attached to a quote, retaining the preceding action.
    scene = re.sub(rf"\b(?:and\s+|then\s+|before\s+)?{speech}\b[^\n\"]*{quote}", "", scene, flags=re.I)
    # Leave quoted non-dialogue content (for example, text on a sign) intact.
    # Speech can follow a visible action whose recipient is the other named
    # character ("Lefty kisses Anna, then says ..."). Keep the action while
    # removing the orphaned speech verb and delivery-only prose.
    scene = re.sub(rf"\bthen\s+{speech}\b", "", scene, flags=re.I)
    scene = re.sub(
        r"\b(?:in|with)\s+(?:an?\s+)?[^.?!]{1,70}?\s+(?:voice|tone)\b",
        "", scene, flags=re.I,
    )
    scene = re.sub(rf"\b({speaker})\s+and\s+(?=(?:walks?|opens?|closes?|turns?|smiles?|sits?|stands?|holds?|looks?|steps?|winks?)\b)", r"\1 ", scene, flags=re.I)
    scene = re.sub(r"\b(?:and\s+){2,}", "and ", scene, flags=re.I)
    scene = re.sub(r"(?<=[.!?])\s+and\s+", " ", scene, flags=re.I)
    # Removing two adjacent speech clauses can leave a bare conjunction.
    scene = re.sub(r"(?:^|(?<=[.!?]))\s*(?:and|then)\b(?=\s*(?:[.!?]|$))", "", scene, flags=re.I)
    # When exact quoted lines are present, a generic "they are talking" cue
    # duplicates those lines and trips the unresolved-vocal validator. Keep its
    # visual implication while leaving speech exclusively in the dialogue turns.
    if dialogue_candidates(scene_text):
        scene = re.sub(
            r"\b(?:they|both characters|the two of them)\s+"
            r"(?:(?:are|were)\s+)?(?:talking|speaking|chatting|conversing)"
            r"(?:\s+(?:to|with)\s+(?:each other|one another))?\b",
            "They face each other", scene, flags=re.I,
        )
    scene = re.sub(r"\s+", " ", scene).strip()
    return scene.strip(" ,.;")


def _multichar_ambience(scene_text):
    """Extract authored non-vocal sound without leaving it in visual blocking."""
    text = non_speech_text(normalize_multichar_dialogue_quotes(scene_text))
    sound_clauses = [
        clause.strip(" ,.;") for clause in re.split(r"[.!?;\n]", text)
        if re.search(r"\b(?:hear|heard|audible|sound(?:s)? of|music|band|instrumental|"
                     r"rain|wind|waves|traffic|fire|thunder|applause)\b", clause, re.I)
        and not re.search(r"\b(?:music|band|jazz|rock|piano|guitar|drums?|song)\b", clause, re.I)
    ]
    if not sound_clauses:
        return "Quiet non-vocal room tone appropriate to the established setting"
    ambience = "; ".join(sound_clauses)
    return ambience + "; any background crowd is inaudible"


def _multichar_diegetic_music(scene_text):
    """Return authored music for the timed audiovisual description, not soundscape."""
    clauses = [
        clause.strip(" ,.;") for clause in re.split(r"[.!?;\n]", scene_text or "")
        if re.search(r"\b(?:music|band|jazz|rock|piano|guitar|drums?|song)\b", clause, re.I)
    ]
    if not clauses:
        return ""
    music = "; ".join(clauses)
    if not re.search(r"\b(?:singing|singer|vocals?|lyrics|chanting)\b", music, re.I):
        music += "; the music is instrumental, without singing or spoken words"
    return music


def _remove_multichar_sound_cues(visual_scene):
    """Keep soundtrack instructions out of image and movement descriptions."""
    clauses = re.split(r"(?<=[.!?;])\s+", visual_scene)
    kept = [clause for clause in clauses if not re.search(
        r"\b(?:we\s+)?(?:hear|heard)\b|\b(?:music|soundtrack)\s+(?:plays?|playing)\b",
        clause, re.I,
    )]
    return " ".join(kept).strip(" ,.;")


def _multichar_performance_hints(scene_text, char_a_name, char_b_name, dialogue_turns):
    """Keep explicit delivery and actions attached to their source turn."""
    scene_text = _expand_multichar_names(scene_text, char_a_name, char_b_name)
    authored = {span.start for span in dialogue_candidates(scene_text)}
    quotes = [m for m in re.finditer(r'["“]([^"”\n]{1,240})["”]', scene_text or "")
              if m.start() in authored][:len(dialogue_turns)]
    delivery_words = (
        "angrily|annoyed|apologetically|breathlessly|calmly|casually|coldly|"
        "excitedly|firmly|gently|gratefully|happily|husky|nervously|playfully|"
        "drunkenly|quietly|resigned|sadly|seductively|softly|sweetly|teasingly|warmly|whimsically"
    )
    hints = []
    for index, exact_turn in enumerate(dialogue_turns):
        key = exact_turn["speaker"]
        match = quotes[index] if index < len(quotes) else None
        prior_end = quotes[index - 1].end() if index and index - 1 < len(quotes) else 0
        prefix = (scene_text or "")[max(prior_end, match.start() - 240):match.start()] if match else ""
        delivery_match = re.findall(rf"\b({delivery_words})\b", prefix, re.I)
        delivery = delivery_match[-1].lower() if delivery_match else "natural conversational delivery"
        if re.search(r"\bwhisper(?:s|ed|ing)?\b", prefix, re.I):
            delivery = "a whisper"
        elif re.search(r"\bshout(?:s|ed|ing)?\b", prefix, re.I):
            delivery = "a shout"
        action_before = ""
        action_after = ""
        if match:
            next_start = quotes[index + 1].start() if index + 1 < len(quotes) else len(scene_text or "")
            tail = (scene_text or "")[match.end():next_start]
            tail_delivery = re.search(
                r"\b(?:in|with)\s+(?:an?\s+)?([^.!?]{1,60}?)\s+(?:voice|tone)\b",
                tail, re.I,
            )
            if tail_delivery:
                delivery = re.sub(r"\s+", " ", tail_delivery.group(1)).strip().lower()
            actor = char_a_name if key == "a" else char_b_name
            attached_vocal = re.search(
                rf"\b{re.escape(actor)}\s+"
                r"(gasps?|sighs?|laughs?|giggles?|moans?|groans?)\s+and\s+"
                rf"{_MULTICHAR_SPEECH}\b[^.?!\n]*$",
                prefix, re.I,
            )
            if attached_vocal:
                cue = attached_vocal.group(1).lower()
                silent_actions = {
                    "gasp": "takes a quick visible breath",
                    "gasps": "takes a quick visible breath",
                    "sigh": "visibly exhales",
                    "sighs": "visibly exhales",
                    "laugh": "smiles broadly",
                    "laughs": "smiles broadly",
                    "giggle": "smiles playfully",
                    "giggles": "smiles playfully",
                    "moan": "reacts with visible pleasure",
                    "moans": "reacts with visible pleasure",
                    "groan": "reacts with visible strain",
                    "groans": "reacts with visible strain",
                }
                action_before = f"{actor} {silent_actions[cue]}"
            before_match = re.search(
                rf"\b{re.escape(actor)}\s+(.+?),?\s+(?:then|before)\s+"
                rf"{_MULTICHAR_SPEECH}\b[^.?!\n]*$",
                prefix, re.I,
            )
            if not action_before and before_match and re.search(
                r"\b(?:approach|dance|embrac|lean|kiss|touch|hold|hand|grasp|squeez|pull|push|"
                r"open|close|remov|turn|look|walk|step|lead|take|wink|nod|wave)\w*\b",
                before_match.group(1), re.I,
            ):
                action_before = f"{actor} {before_match.group(1).strip()}"
            # Direct coordination also establishes ordering: "Frankie points
            # to the dogs and says ..." means the visible action completes
            # silently before the exact authored dialogue begins.
            direct_match = re.search(
                rf"\b{re.escape(actor)}\s+(.+?)\s+and\s+"
                rf"{_MULTICHAR_SPEECH}\b[^.?!\n]*$",
                prefix, re.I,
            )
            if not action_before and direct_match and re.search(
                r"\b(?:approach|dance|embrac|lean|kiss|touch|hold|hand|grasp|squeez|pull|push|"
                r"open|close|remov|turn|look|walk|sit|stand|step|lead|take|point|place|put|"
                r"smile|wink|nod|wave)\w*\b",
                direct_match.group(1), re.I,
            ):
                action_before = f"{actor} {direct_match.group(1).strip()}"
            # A complete action sentence immediately before the speaker cue
            # establishes ordering too (not only "A walks, then says ...").
            if not action_before:
                separated = re.search(
                    rf"\b{re.escape(actor)}\s+([^.!?\n]+)[.!?]\s*"
                    rf"{re.escape(actor)}\s+{_MULTICHAR_SPEECH}\b[^.!?\n]*$",
                    prefix, re.I,
                )
                if separated and re.match(
                    r"(?:walks?|steps?|sits?|stands?|turns?|opens?|closes?|"
                    r"approaches?|puts?|places?|takes?|nods?|waves?|leans?)\b",
                    separated.group(1), re.I,
                ):
                    action_before = f"{actor} {separated.group(1).strip()}"
            action = re.search(
                r"\band\s+((?:walks?|opens?|closes?|turns?|smiles?|sits?|stands?|holds?|"
                r"looks?|steps?|winks?|nods?|waves?)\b[^.?!,;]*)",
                tail, re.I,
            )
            if action:
                recipient = char_b_name if key == "a" else char_a_name
                action_text = re.sub(r"\b(?:her|him)\b", recipient,
                                     action.group(1).strip(), flags=re.I)
                action_after = f"{actor} {action_text}"
            if index == len(quotes) - 1:
                trailing = re.match(
                    rf"\s*[.!?]?\s*(?:Then\s+)?({re.escape(actor)}|She|He)\s+"
                    r"((?:takes?|leads?|walks?|opens?|closes?|turns?|steps?|waves?|nods?)\b[^.!?]*)",
                    tail, re.I,
                )
                if trailing:
                    action_after = f"{actor} {trailing.group(2).strip()}"
        hints.append({
            "speaker": key,
            "line": exact_turn["line"],
            "delivery": delivery,
            "action_before": action_before,
            "action_after": action_after,
        })
    return hints


def multichar_camera_profile(scene_text):
    """Choose restrained camera motion from requested blocking."""
    text = non_speech_text(scene_text)
    # Explicit camera instructions outrank motion-derived defaults.
    explicit = [clause.strip() for clause in re.split(r"[.;\n]", text)
                if re.search(r"\b(?:camera|pan|pans|zoom|zooms|fixed shot|cuts?)\b", clause, re.I)
                and re.search(r"\b(?:pan|pans|zoom|zooms|fixed|static|locked|cuts?)\b|\bcamera\s+(?:\w+\s+){0,2}moves?\b", clause, re.I)]
    if explicit:
        return "Requested camera behavior: " + "; ".join(explicit)
    if re.search(r"\b(?:walk|walking|stroll|strolling)\b", text, re.I):
        return "A gentle tracking shot follows both subjects together while keeping both subjects in frame"
    if re.search(r"\b(?:enters?|entering|exits?|exiting|arrives?|depart(?:s|ing)?)\b", text, re.I):
        return "A stable medium-wide shot keeps both subjects and the entrance or exit path visible"
    if re.search(r"\b(?:sit|sitting|seated)\b", text, re.I):
        return "A stable medium two-shot holds both faces, with a very slow small-amplitude push in"
    if re.search(r"\b(?:embrac|kiss|touch|hold|grasp|squeez|intimate)\w*\b", text, re.I):
        return "A locked medium shot keeps both subjects and their interaction continuously visible"
    return "A stable medium two-shot preserves the composition of Picture 3"


def _fallback_multichar_scene_plan(scene_text, char_a_name, char_b_name, lines, order,
                                   authoritative_turns=None, *, continuation=False):
    """Build a safe plan without inventing or repeating dialogue."""
    if authoritative_turns is None:
        authoritative_turns = _multichar_performance_hints(
            scene_text, char_a_name, char_b_name,
            [{"speaker": key, "line": lines[key]} for key in order],
        )
    silent_keys = performance_plan.silent_cast(scene_text, {"a": char_a_name, "b": char_b_name})
    authoritative_turns = [t for t in authoritative_turns if t["speaker"] not in silent_keys]
    source_scene = _expand_multichar_names(scene_text, char_a_name, char_b_name)
    for turn in authoritative_turns or []:
        action = turn.get("action_after", "")
        actor = char_a_name if turn["speaker"] == "a" else char_b_name
        if action.startswith(actor + " "):
            suffix = re.escape(action[len(actor) + 1:])
            source_scene = re.sub(
                rf"(?:{re.escape(actor)}|She|He)\s+{suffix}", "", source_scene, flags=re.I,
            )
    visual_with_sound = _multichar_visual_scene(source_scene, char_a_name, char_b_name)
    visual_scene = _remove_multichar_sound_cues(visual_with_sound)
    for turn in authoritative_turns or []:
        before = turn.get("action_before", "").rstrip(" ,")
        if before:
            visual_scene = re.sub(
                re.escape(before) + r"(?:\s+(?:then|before)\b)?", "", visual_scene, count=1, flags=re.I)

    # Attached actions live on their dialogue turn; remove their duplicate prose
    # from the general scene so H3 receives each action once at the right time.
    for source_turn in authoritative_turns or []:
        action = source_turn.get("action_after") or ""
        if action:
            recipient = char_b_name if source_turn["speaker"] == "a" else char_a_name
            pattern = re.escape(action).replace(re.escape(recipient), rf"(?:{re.escape(recipient)}|her|him|them)")
            visual_scene = re.sub(
                pattern, "", visual_scene, count=1, flags=re.I,
            )
    visual_scene = re.sub(r"(?:^|(?<=[.!?]))\s*(?:then|and)\s*(?=[.!?]|$)", "", visual_scene, flags=re.I)
    visual_scene = re.sub(r"\s+", " ", visual_scene).strip(" .")
    if not visual_scene:
        visual_scene = "Both subjects remain in the established setting"
    turns = []
    for source_turn in (authoritative_turns if authoritative_turns is not None else [
        {"speaker": key, "line": lines[key], "delivery": "natural conversational delivery",
         "action_before": "", "action_after": ""} for key in order
    ]):
        key = source_turn["speaker"]
        name = char_a_name if key == "a" else char_b_name
        turn = dict(source_turn)
        turn["speaker_name"] = name
        turns.append(turn)
    plan = {
        "visual_scene": visual_scene,
        "camera": multichar_camera_profile(scene_text),
        "ambience": _multichar_ambience(visual_with_sound),
        "turns": turns,
        "planner_status": "fallback",
        "fallback_reason": "deterministic_default",
    }
    if continuation:
        plan["camera"] = "Begin with the camera position, angle, framing and scale from the supplied final frame. " + plan["camera"]

    _require_multichar_scene_plan(plan, turns)
    image_scene = visual_scene
    # The image establishes the opening state, never the final state of a
    # gesture authored later in the shot.
    intent = performance_plan.make_intent(
        normalize_multichar_dialogue_quotes(scene_text),
        {"a": char_a_name, "b": char_b_name}, visual_scene, image_scene,
        plan["camera"], plan["ambience"], turns,
        _multichar_diegetic_music(visual_with_sound),
        continuation=continuation,
    )
    return performance_plan.complete_fallback(intent)


def _validate_multichar_scene_plan(plan, authoritative_turns):
    if not isinstance(plan, dict):
        return False
    if plan.get("version") == performance_plan.VERSION:
        intent = plan.get("intent")
        if not isinstance(intent, dict):
            return False
        if performance_plan.validate(plan, intent):
            return False
        for actual, expected in zip(plan["turns"], authoritative_turns):
            for field in ("speaker", "line", "action_before", "action_after"):
                if actual.get(field) != expected.get(field):
                    return False
            delivery = expected.get("delivery", "natural conversational delivery")
            if delivery != "natural conversational delivery" and actual["delivery"] != delivery:
                return False
        return len(plan["turns"]) == len(authoritative_turns)
    if not isinstance(plan.get("visual_scene"), str) or not plan["visual_scene"].strip():
        return False
    if any(turn["line"] and turn["line"] in plan["visual_scene"]
           for turn in authoritative_turns):
        return False
    for field in ("visual_scene", "camera", "ambience"):
        value = plan.get(field)
        if not isinstance(value, str) or has_unresolved_speech(value, _MULTICHAR_VOCAL):
            return False
        if re.search(r"<[a-z/][^>]*>", value, re.I):
            return False
    turns = plan.get("turns")
    if not isinstance(turns, list) or len(turns) != len(authoritative_turns):
        return False
    for turn, expected in zip(turns, authoritative_turns):
        if not isinstance(turn, dict):
            return False
        if turn.get("speaker") not in ("a", "b") or not isinstance(turn.get("line"), str):
            return False
        if re.search(r"<[^>]*>", turn["line"]):
            return False
        for field in ("action_before", "action_after"):
            if (not isinstance(turn.get(field), str)
                    or has_unresolved_speech(turn[field], _MULTICHAR_VOCAL)
                    or re.search(r"<[^>]*>", turn[field])):
                return False
        if not isinstance(turn.get("delivery"), str) or re.search(r"<[^>]*>", turn["delivery"]):
            return False
        if any(turn.get(field) != expected[field] for field in (
            "speaker", "line", "delivery", "action_before", "action_after",
        )):
            return False
    return True


def build_multichar_scene_plan(char_a_name, char_b_name, scene_text, lines, order,
                               model=None, keep_alive=None, dialogue_turns=None):
    """Compose immutable intent with validated bounded performance enrichment."""
    exact_turns = dialogue_turns if dialogue_turns is not None else [
        {"speaker": key, "line": lines[key]} for key in order
    ]
    authoritative = _multichar_performance_hints(
        scene_text, char_a_name, char_b_name, exact_turns,
    )
    fallback = _fallback_multichar_scene_plan(
        scene_text, char_a_name, char_b_name, lines, order, authoritative,
    )
    return performance_plan.plan_with_model(
        fallback, call_ollama, find_first_json_object, model=model, keep_alive=keep_alive,
    )


def build_multichar_image_instruction(char_a_name, char_a_concept, char_b_name,
                                      char_b_concept, plan,
                                      char_a_picture=1, char_b_picture=2, *, latest_request=None):
    """Use the same validated plan for the composited image and target video."""
    actions = [plan.get('image_scene', plan['visual_scene'])]
    for turn in ([] if plan.get('version') == performance_plan.VERSION else plan.get('turns', [])):
        for field in ('action_before', 'action_after'):
            action = (turn.get(field) or '').strip()
            if action and action.casefold() not in plan['visual_scene'].casefold():
                actions.append(action)
    instruction = build_image_prompt(
        assemble_final_prompt(*actions),
        identities=((char_a_picture, char_a_name), (char_b_picture, char_b_name)),
        scene_index=1 if (char_a_picture, char_b_picture) == (2, 3) else None,
        latest_request=latest_request, adult_mode=get_content_mode() == 'nsfw',
        character_metadata={char_a_name: {'concept': char_a_concept},
                            char_b_name: {'concept': char_b_concept}},
    )
    # Two identity references define the principals, not a ban on explicitly
    # authored extras. Job 687 requests crew working behind those principals.
    if re.search(r"\b(?:rest of the crew|background crew)\b", plan.get('image_scene', ''), re.I):
        instruction = re.sub(
            r"Show exactly two ([^.]+)\.",
            r"Show exactly two \1 as the named principal characters. Include the explicitly requested background crew.",
            instruction, count=1,
        )
    return instruction


def _deterministic_multichar_h3_prompt(char_a_name, char_a_concept, char_b_name,
                                        char_b_concept, scene_text, lines, order,
                                        dialogue_turns=None,
                                        scene_plan=None):
    if scene_plan is not None:
        plan = scene_plan
    else:
        exact_turns = dialogue_turns if dialogue_turns is not None else [
            {"speaker": key, "line": lines[key]} for key in order
        ]
        authoritative = _multichar_performance_hints(
            scene_text, char_a_name, char_b_name, exact_turns,
        )
        plan = _fallback_multichar_scene_plan(
            scene_text, char_a_name, char_b_name, lines, order,
            authoritative_turns=authoritative,
        )
    expected_turns = dialogue_turns if dialogue_turns is not None else [
        {"speaker": key, "line": lines[key]} for key in order
    ]
    silent_keys = performance_plan.silent_cast(scene_text, {"a": char_a_name, "b": char_b_name})
    expected_turns = [t for t in expected_turns if t["speaker"] not in silent_keys]
    _require_multichar_scene_plan(plan, _multichar_performance_hints(
        scene_text, char_a_name, char_b_name, expected_turns,
    ))
    if plan.get("version") == performance_plan.VERSION:
        # Reconstruct the immutable contract from the request, rather than
        # trusting a caller-provided plan's copy of its own intent.
        expected = _fallback_multichar_scene_plan(
            scene_text, char_a_name, char_b_name, lines, order,
            _multichar_performance_hints(scene_text, char_a_name, char_b_name, expected_turns),
            continuation=plan["intent"].get("continuation", False),
        )
        errors = performance_plan.validate(plan, expected["intent"])
        if errors:
            raise ValueError("Invalid performance plan: " + "; ".join(errors))
        body = performance_plan.compile_prompt(plan)
        performance_plan.validate_compiled(body, plan)
        return body
    dialogue = []
    speaking_keys = list(dict.fromkeys(turn["speaker"] for turn in plan["turns"]))
    speaker_ids = {key: f"S{index + 1}" for index, key in enumerate(speaking_keys)}
    audio_ids = {key: index + 1 for index, key in enumerate(speaking_keys)}
    for turn in plan["turns"]:
        key = turn["speaker"]
        name = char_a_name if key == "a" else char_b_name
        audio = str(audio_ids[key])
        subject = "1" if key == "a" else "2"
        speaker_id = speaker_ids[key]
        before = (turn.get("action_before") or "").strip()
        after = (turn.get("action_after") or "").strip()
        delivery = (turn.get("delivery") or "natural conversational delivery").strip()
        if delivery.lower() == "natural conversational delivery":
            delivery_phrase = "speaks naturally"
        elif delivery.lower().endswith("ly"):
            delivery_phrase = f"speaks {delivery}"
        elif delivery.lower().startswith(("a ", "an ")):
            delivery_phrase = f"speaks with {delivery}"
        else:
            article = "an" if delivery[:1].lower() in "aeiou" else "a"
            delivery_phrase = f"speaks in {article} {delivery} tone"
        before_text = (
            f"{before}. Both subjects remain silent during this action. After it is complete, "
            if before else ""
        )
        after_text = f" After the line, {after}." if after else ""
        dialogue.append(
            before_text
            + f'<Subject {subject}> ({speaker_id}) {delivery_phrase}, using the voice '
              f'timbre referenced from <Audio {audio}>: <d>[English] {turn["line"]}</d>. '
              "Lip movement begins with the first word and ends with the last."
            + after_text
        )
    visual_scene = plan["visual_scene"].strip().rstrip(".")
    camera = plan["camera"].strip().rstrip(".")
    ambience = plan["ambience"].strip().rstrip(".")
    cleaned_source = _multichar_visual_scene(scene_text, char_a_name, char_b_name)
    diegetic_music = _multichar_diegetic_music(cleaned_source)
    music_timeline = (f" Diegetic music remains audible during [Shot 1]: {diegetic_music}."
                      if diegetic_music else "")
    if not plan["turns"]:
        return (
            "subject_definitions:\n"
            f"<Subject 1> is {char_a_name}, whose visual identity comes from <Picture 1>.\n"
            f"<Subject 2> is {char_b_name}, whose visual identity comes from <Picture 2>.\n"
            "<Picture 3> is the target composition and opening-scene reference.\n\n"
            "summary:\n[reference generation] Preserve both character identities while developing "
            "the composition in <Picture 3>. This is a silent scene with no speech or vocalization.\n\n"
            "retention_analysis:\n"
            "<Subject 1>: fully_preserved - identity remains consistent with <Picture 1>.\n"
            "<Subject 2>: fully_preserved - identity remains consistent with <Picture 2>.\n"
            "<Picture 3>: fully_preserved - framing and visible scene state establish the opening.\n\n"
            "detailed_description:\n"
            "The target video is live-action and cinematic. "
            f"[Shot 1] Begin from <Picture 3>. {visual_scene}. {camera}."
            f"{music_timeline} "
            "Continue in one uninterrupted shot. Both subjects remain silent with closed mouths; "
            "there is no speech, whispering, singing, laughter, moaning, or other vocalization."
            f"\n\noverall_soundscape: {ambience}; non-vocal ambience only."
            "\n\nnon_diegetic_music: N/A"
        )
    return (
        "subject_definitions:\n"
        f"<Subject 1> is {char_a_name}, whose visual identity comes from <Picture 1>.\n"
        f"<Subject 2> is {char_b_name}, whose visual identity comes from <Picture 2>.\n"
        "<Picture 3> is the target composition and opening-scene reference.\n"
        + "".join(
            f"<Audio {audio_ids[key]}> is the voice-timbre reference for <Subject {1 if key == 'a' else 2}> "
            f"({speaker_ids[key]}); its recorded words are excluded from the target soundtrack.\n"
            for key in speaking_keys
        )
        + "\n"
        "summary:\nContinue naturally from <Picture 3> in one uninterrupted cinematic shot. "
        "Preserve both identities and use each audio sample only for its assigned voice timbre.\n\n"
        "retention_analysis:\n"
        "<Subject 1>: fully_preserved from <Picture 1>.\n"
        "<Subject 2>: fully_preserved from <Picture 2>.\n"
        "<Picture 3>: fully_preserved as the opening composition, including positions, clothing, "
        "setting, lighting, and props.\n"
        + "".join(
            f"<Audio {audio_ids[key]}>: reference - voice timbre only; its recorded words are not copied.\n"
            for key in speaking_keys
        )
        + "\n"
        "detailed_description:\n"
        f"[Shot 1] Begin exactly from <Picture 3> with both subjects visible in their established positions. "
        f"{visual_scene}. {camera}."
        f"{music_timeline} "
        "Perform these authored turns in order: "
        + " ".join(dialogue)
        + " The other subject listens without speaking. There is no human speech or vocalization outside "
          "the authored dialogue. After the final line, both characters remain naturally engaged without "
          "further speech. Keep the existing camera position and continue without a cut."
        + f"\n\noverall_soundscape: {ambience}; no additional human speech or vocalizations."
          "\n\nnon_diegetic_music: N/A"
    )


def normalize_multichar_dialogue_quotes(scene_text):
    """Normalize supported quotes and repair paired replacement markers.

    A few Telegram/Windows input paths have converted curly quote bytes into
    U+FFFD before the job reaches the worker. Only paired markers around a
    plausible dialogue line are treated as quotes so unrelated damaged text is
    left visible rather than silently rewritten.
    """
    text = normalize_quotes(scene_text)
    return re.sub(r'\ufffd([^\ufffd\n]{1,240})\ufffd', r'"\1"', text)


def _near_character_name(token, character_name):
    """Accept a one-character typo in a selected character's given name."""
    candidate = re.sub(r"[^a-z0-9]", "", token.lower())
    expected = re.sub(r"[^a-z0-9]", "", character_name.lower().split()[0])
    if candidate == expected:
        return True
    if min(len(candidate), len(expected)) < 4 or abs(len(candidate) - len(expected)) > 1:
        return False
    if len(candidate) == len(expected):
        return sum(a != b for a, b in zip(candidate, expected)) == 1
    longer, shorter = (candidate, expected) if len(candidate) > len(expected) else (expected, candidate)
    return any(longer[:index] + longer[index + 1:] == shorter for index in range(len(longer)))


def extract_multichar_dialogue_turns(scene_text, char_a_name, char_a_concept,
                                     char_b_name, char_b_concept):
    """Return every quoted line as an ordered, speaker-bound turn."""
    scene_text = normalize_multichar_dialogue_quotes(scene_text)
    authored = {span.start for span in dialogue_candidates(scene_text)}
    matches = [m for m in re.finditer(r'[\"]([^\"]{1,240})[\"]', scene_text)
               if m.start() in authored]
    if not matches:
        return []
    female_a = bool(re.search(r"\b(?:woman|girl|female|she|her)\b", char_a_concept or "", re.I))
    female_b = bool(re.search(r"\b(?:woman|girl|female|she|her)\b", char_b_concept or "", re.I))
    assigned = []
    previous_end = 0
    for match in matches:
        prefix = (scene_text or "")[max(previous_end, match.start() - 240):match.start()]
        speech = _MULTICHAR_SPEECH
        names = rf"(?:{re.escape(char_a_name)}|{re.escape(char_b_name)}|he|she)"
        clause = re.split(r"[.?!]\s*", prefix)[-1]
        leading = re.match(rf"\s*({names})\b", clause, re.I)
        # Prefer a direct grammatical subject close to the speech verb. If the
        # verb has an omitted subject after "then", retain the leading actor of
        # that clause rather than the action's recipient nearest to "says".
        colon = re.search(rf"\b({names})\s*:\s*$", clause, re.I)
        token = colon.group(1).lower() if colon else ""
        token = token or (leading.group(1).lower() if leading and re.search(
            rf"\b{speech}\b[^.?!\n]*$", clause, re.I,
        ) else "")
        if not token:
            direct = re.findall(
                rf"\b({names})\b(?:\s+\w+){{0,2}}\s+{speech}\b[^.?!\n]*$",
                prefix, re.I,
            )
            token = direct[-1].lower() if direct else ""
        if not token and re.search(rf"\bthen\s+{speech}\b[^.?!\n]*$", prefix, re.I):
            token = leading.group(1).lower() if leading else ""
        if not token:
            speakers = re.findall(
                rf"\b({names})\b[^.?!\n]{{0,35}}\b{speech}\b",
                prefix, re.I,
            )
            token = speakers[-1].lower() if speakers else ""
        if not token:
            suffix = (scene_text or "")[match.end():match.end() + 100]
            post = re.match(
                rf"\s*,?\s*{speech}\s+({names})\b", suffix, re.I,
            )
            token = post.group(1).lower() if post else ""
        if not token and re.search(rf"\b{speech}\b", prefix, re.I):
            # Recover a selected name with a single typo, such as Lyla/Lylah.
            # Use the first matching name in the current clause: in "A touches
            # B, then says", A is the grammatical actor and B the recipient.
            clause = re.split(r"[.?!]\s*", prefix)[-1]
            for candidate in re.findall(r"\b[A-Za-z][A-Za-z'-]{2,30}\b", clause):
                if (_near_character_name(candidate, char_a_name)
                        or _near_character_name(candidate, char_b_name)):
                    token = candidate.lower()
                    break
        if _near_character_name(token, char_a_name) or token == "she" and female_a and not female_b or token == "he" and not female_a and female_b:
            assigned.append("a")
        elif _near_character_name(token, char_b_name) or token == "she" and female_b and not female_a or token == "he" and not female_b and female_a:
            assigned.append("b")
        else:
            assigned.append(None)
        previous_end = match.end()
    for index, key in enumerate(assigned):
        if key is None:
            if index and assigned[index - 1] in ("a", "b"):
                assigned[index] = "b" if assigned[index - 1] == "a" else "a"
            elif len(assigned) == 2 and assigned.count(None) == 1:
                assigned[index] = "b" if "a" in assigned else "a"
    if len(assigned) == 2 and assigned[0] == assigned[1]:
        between = (scene_text or "")[matches[0].end():matches[1].start()]
        if re.search(r"\brepl(?:y|ies|ied)\b", between, re.I):
            assigned[0] = "b" if assigned[1] == "a" else "a"
    return [
        {"speaker": key, "line": match.group(1).strip()}
        for key, match in zip(assigned, matches) if key in ("a", "b")
    ]


def extract_multichar_spoken_lines(scene_text, char_a_name, char_a_concept,
                                   char_b_name, char_b_concept):
    """Compatibility adapter for legacy two-line callers."""
    turns = extract_multichar_dialogue_turns(
        scene_text, char_a_name, char_a_concept, char_b_name, char_b_concept,
    )[:2]
    lines = {turn["speaker"]: turn["line"] for turn in turns}
    return lines.get("a"), lines.get("b"), [turn["speaker"] for turn in turns]


def multichar_video_duration(dialogue_turns, scene_plan=None):
    """Budget speech, turn changes, delivery, and visible actions within H3 limits."""
    if (scene_plan or {}).get("version") == performance_plan.VERSION:
        return scene_plan["duration_seconds"]
    words = sum(len(re.findall(r"[A-Za-z0-9']+", turn.get("line", "")))
                for turn in dialogue_turns)
    turn_gap = max(0, len(dialogue_turns) - 1) * 0.35
    delivery_time = sum(
        0.25 for turn in (scene_plan or {}).get("turns", [])
        if turn.get("delivery") not in ("", "natural conversational delivery")
    )
    action_time = sum(
        min(5.0, 1.5
            + len(re.findall(
                r"\b(?:approach|danc|lean|kiss|touch|hold|grasp|squeez|pull|push|"
                r"open|close|turn|look|walk|sit|stand|step|point|place|put|smile|"
                r"wink|nod|wave)\w*\b", action, re.I,
            )) * 1.25
            + len(re.findall(r"[A-Za-z0-9']+", action)) * 0.1)
        for turn in (scene_plan or {}).get("turns", [])
        for action in (turn.get("action_before", ""), turn.get("action_after", ""))
        if action
    )
    visual_scene = (scene_plan or {}).get("visual_scene", "")
    visual_actions = len(re.findall(
        r"\b(?:approach|danc|lean|kiss|touch|hold|grasp|squeez|pull|push|"
        r"open|close|turn|look|step|wink|nod|wave)\w*\b",
        visual_scene, re.I,
    ))
    visual_time = min(3.0, visual_actions * 0.75)
    return min(10.0, max(5.0, round(
        1.0 + words / 3.0 + turn_gap + delivery_time + action_time + visual_time, 1
    )))


def multichar_dialogue_duration(spoken_line_a, spoken_line_b):
    """Compatibility wrapper for legacy two-line callers."""
    turns = [{"line": line} for line in (spoken_line_a, spoken_line_b) if line]
    return multichar_video_duration(turns)

def enhance_scene(raw_scene: str, character_name: str = "the character",
                  spoken_line: str = None, keep_alive: str = None) -> str:
    """Describe the opening frame from source-owned instructions.

    Workflow aesthetics and the identity reference still supply rendering style.
    A prose LLM rewrite must not invent state that the video would then inherit.
    """
    contract = VideoRequest.create(raw_scene, character_name, spoken_line)
    return (
        "Create the opening frame immediately before the first requested action, not its completed "
        "result. Follow the requested composition and timeline. No subtitles, captions, dialogue text, or text overlays. "
        + contract.visual()
    )


def find_first_json_object(s: str):
    """Extract the FIRST complete, valid JSON object from a string. Handles the
    case where Ollama returns SEVERAL JSON objects concatenated (e.g.
    '{"a":1}\n\n{"a":2}') — plain json.loads fails on that and would otherwise
    leak the raw blob. Returns the dict, or None if no valid object found.
    """
    if not s:
        return None
    i = 0
    n = len(s)
    while i < n:
        # find the start of the next possible object
        start = s.find("{", i)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        j = start
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = s[start:j + 1]
                        try:
                            return json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            break  # malformed; try the next {
            j += 1
        i = start + 1
    return None


def find_first_json_array(s: str):
    """Extract the FIRST complete, valid JSON array from a string. Mirrors
    find_first_json_object but scans for '[' ... ']' instead of '{' ... '}'.
    Returns the list, or None if no valid array is found."""
    if not s:
        return None
    i = 0
    n = len(s)
    while i < n:
        start = s.find("[", i)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        j = start
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        candidate = s[start:j + 1]
                        try:
                            return json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            break  # malformed; try the next [
            j += 1
        i = start + 1
    return None


def generate_look_variants_llm(concept: str, n: int = 3, personality_notes: str = "") -> tuple:
    """LLM-driven LOOK-candidate generator. Given a user concept (e.g. "tall
    Caucasian busty blonde"), ask the active Ollama model to produce `n`
    DIFFERENT people who ALL fit the concept — each matching every explicit
    constraint (ethnicity, height, body type, hair color, age band...) but
    clearly distinct as individuals (facial features, hairstyle within the
    stated hair color, eye color, skin undertone, exact proportions,
    subtle age).

    Returns a tuple (variants, stylized): variants is the list of n description
    strings for comfy_client.generate_look_variants; stylized is True when the
    concept EXPLICITLY requests a cartoon/anime/illustrated/etc. style (so the
    renderer should NOT force a photorealistic medium tag)."""
    stylized = any(
        t in concept.lower()
        for t in ("cartoon", "anime", "animated", "mascot", "illustration", "illustrated",
                  "painting", "sketch", "drawing", "2d", "cell-shaded",
                  "cell shaded", "stylized", "chibi", "comic", "manga")
    )
    # Look candidates establish the reusable identity reference. Keep this step
    # SFW even when the chat is in NSFW mode; subsequent media still follows the
    # job's content-mode snapshot.
    taste = (
        "Keep every look candidate non-explicit and wearing clothing. Sexy and "
        "revealing outfits such as bikinis, lingerie, crop tops, or similarly "
        "scanty clothing are allowed when they fit the concept, but nipples and "
        "genitals must remain covered and the candidate must not depict sexual activity. "
        "Preserve the character's adult appearance, provocative style, and personality "
        "cues rather than making an adult concept wholesome or conservative."
    )
    look_schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": n,
                "maxItems": n,
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }
    expression = build_expression_instruction(concept, personality_notes)
    prompt = (
        f"Create {n} distinct adult look candidates for this concept: {concept}\n"
        f"Personality: {personality_notes}\n"
        "Preserve explicitly specified physical traits, ethnicity, skin tone and clothing. "
        "Do not infer ethnicity from hair or eye color. Vary unspecified facial features "
        "and hairstyles within the concept. Use fitting clothes and a concrete natural setting. "
        "Match explicitly requested art styles; otherwise use photographic descriptions. "
        "Keep anatomy believable. Do not invent extreme body proportions. "
        "Use the requested expression if specified; otherwise leave facial expression and "
        "posture to the following finishing direction, without adding competing expressions: "
        f"{expression}\n{taste}\n"
        f"Return a JSON object with candidates: exactly {n} nonempty descriptive strings."
    )

    def parse_candidates(raw: str):
        parsed = None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = find_first_json_object(raw) or find_first_json_array(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("candidates")
        if not isinstance(parsed, list):
            return None
        candidates = [x.strip() for x in parsed if isinstance(x, str) and x.strip()]
        return candidates if len(candidates) == n else None

    raw = call_ollama(prompt, json_mode=look_schema).strip()
    candidates = parse_candidates(raw)
    if candidates is None:
        print("[worker] look candidate planner returned invalid structure; retrying once")
        repair_prompt = (
            prompt + "\nThe prior response was invalid. Return the required JSON object "
            f"with exactly {n} non-empty candidate strings."
        )
        raw = call_ollama(repair_prompt, json_mode=look_schema).strip()
        candidates = parse_candidates(raw)
    if candidates is None:
        raise RuntimeError(
            "look candidate planning returned invalid structured output after retry"
        )
    return ([assemble_final_prompt(candidate, expression) for candidate in candidates], stylized)


def _mentions_other_person(text: str) -> bool:
    """True when the RAW request itself names a second participant (man, guy,
    boyfriend, crowd, another woman, etc.). Pronouns are deliberately excluded:
    'she/her' refer to the character herself in nearly every request, so they
    must not count as evidence of a second person."""
    if not text:
        return False
    return bool(re.search(
        r"\b(man|men|male|guy|guys|boyfriend|husband|lover|lovers|partner|"
        r"partners|dude|bro|bros|couple|threesome|foursome|gangbang|group|"
        r"orgy|audience|crowd|onlookers?|stranger|strangers|another ?(?:wo)?m[ae]n|"
        r"second (?:wo)?m[ae]n|someone|somebody|other people)\b",
        text, re.I,
    ))


def extract_spoken_line(text: str):
    """If the user embedded a spoken line in their video request (e.g. inside
    double/single quotes: she says \"...\"), pull it out and return (scene_clean,
    spoken_line). Returns (text, None) when there is no quoted dialogue.

    The scene text is ALSO cleaned of the dialogue so the image/video scene
    describes the ACTION only, while the extracted line is passed to the H3
    prompt as the exact spoken words (preserving the user's instruction, which
    would otherwise be dropped by enhance_scene's prose-only output).

    Self-healing: a message sent before the closing quote was typed (e.g.
    ...to the camera \"come suck on these for m) previously lost the line AND
    leaked the dangling fragment into the scene prose. Now an unclosed quote
    after a speech verb yields everything after it as the spoken line."""
    if not text:
        return text, None

    def _clean_scene(scene: str) -> str:
        scene = re.sub(
            r"(?i)\b(says|say|said|speaks?|saying|whispers?|whispering|moans?|"
            r"moaning|gasps?|gasping|sighs?|sighing|tells?|telling|murmurs?|"
            r"murmuring|mutters?|muttering|asks?|asking)\b\s*"
            r"(?:(?:in|in?to|towards|to|the)\s+)*camera\b\s*[,:]?\s*$",
            "", scene,
        ).strip()
        return re.sub(r"\s{2,}", " ", scene).strip()

    valid_starts = {span.start for span in speech_spans(text)}
    for q_open, q_close in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        start = text.find(q_open)
        if start < 0:
            continue
        # Apostrophe guard for straight single quotes: "she's" must not open dialogue.
        # (Curly quotes are unambiguous open/close, so no guard needed for them.)
        if q_open == "'" and start > 0 and (text[start - 1].isalnum() or text[start - 1] == "'"):
            continue
        end = text.find(q_close, start + 1)
        if end >= 0:
            if start not in valid_starts:
                continue
            line = text[start + len(q_open):end].strip()
            if line:
                scene = _clean_scene((text[:start] + " " + text[end + len(q_close):]).strip())
                if scene:
                    return scene, line
                return text[:start].strip() or text[end + len(q_close):].strip(), line
        else:
            # Unclosed quote: treat the remainder as the line when there is a
            # speech context before it and the fragment has >= 2 words (a stray
            # apostrophe/quote mid-scene shouldn't hijack the whole message).
            candidate = text[start + len(q_open):].strip()
            pre = text[:start]
            has_speech_context = bool(re.search(
                r"(?i)\b(says?|said|speaks?|speaking|saying|whisper|whispers|"
                r"whispering|moans?|moaning|gasps?|gasping|sighs?|sighing|"
                r"murmurs?|mutters?|tells?|telling|asks?|asking|to the camera|"
                r"in ?to the camera)\b", pre))
            if candidate and len(candidate.split()) >= 2 and has_speech_context:
                scene = _clean_scene(pre.strip())
                return scene, candidate
    return text, None


def process_multichar_video(job, completion_client, current_model):
    """Shared production render path; lease/progress/completion stay with the VPS."""
    payload = job["payload"]
    continuation_path = payload.get("continuation_relative_path")
    char_a = payload["char_a"]
    char_b = payload["char_b"]
    raw_scene_original = payload.get("scene_text") or ""
    raw_scene = normalize_multichar_dialogue_quotes(raw_scene_original)
    interaction_text = normalize_multichar_dialogue_quotes(
        payload.get("interaction_text") or ""
    )
    spoken_line_a = payload.get("spoken_line_a")
    spoken_line_b = payload.get("spoken_line_b")
    dialogue_turns = payload.get("dialogue_turns")
    if not isinstance(dialogue_turns, list) or any(
        not isinstance(turn, dict)
        or turn.get("speaker") not in ("a", "b")
        or not isinstance(turn.get("line"), str)
        for turn in dialogue_turns
    ):
        dialogue_turns = None
    if dialogue_turns is None and (spoken_line_a or spoken_line_b):
        dialogue_turns = [
            {"speaker": key, "line": line}
            for key, line in (("a", spoken_line_a), ("b", spoken_line_b)) if line
        ]
    if dialogue_turns is None:
        dialogue_turns = extract_multichar_dialogue_turns(
            raw_scene, char_a["name"], char_a.get("concept") or "",
            char_b["name"], char_b.get("concept") or "",
        )
    silent_keys = performance_plan.silent_cast(raw_scene, {"a": char_a["name"], "b": char_b["name"]})
    dialogue_turns = [t for t in dialogue_turns if t["speaker"] not in silent_keys]
    if not dialogue_turns and dialogue_candidates(raw_scene) and not silent_keys:
        raise ValueError(
            "Quoted dialogue was found, but its speaker could not be matched to "
            f"{char_a['name']} or {char_b['name']}. Name the speaker before the quote."
        )
    exact_lines = {"a": None, "b": None}
    for turn in dialogue_turns:
        if exact_lines[turn["speaker"]] is None:
            exact_lines[turn["speaker"]] = turn["line"]
    spoken_line_a, spoken_line_b = exact_lines["a"], exact_lines["b"]
    dialogue_order = [turn["speaker"] for turn in dialogue_turns]
    print(f"[worker] job {job['id']}: multi-character video ({char_a['name']} + {char_b['name']})...")
    print(f"[worker] job {job['id']}: raw scene text: {raw_scene[:200]}")

    # Both named actors and their authored timeline remain source-owned.
    enhanced_scene = raw_scene

    # Build the identity-locked image instruction with both characters
    concept_a = char_a.get("concept") or ""
    concept_b = char_b.get("concept") or ""
    if continuation_path:
        authoritative_turns = _multichar_performance_hints(
            raw_scene, char_a["name"], char_b["name"], dialogue_turns,
        )
        scene_plan = _fallback_multichar_scene_plan(
            raw_scene,
            char_a["name"], char_b["name"], exact_lines, dialogue_order,
            authoritative_turns=authoritative_turns,
            continuation=True,
        )
        scene_plan["planner_status"] = "deterministic_continuation"
        scene_plan["fallback_reason"] = ""
    else:
        scene_plan = build_multichar_scene_plan(
            char_a["name"], char_b["name"], raw_scene,
            exact_lines, dialogue_order, model=current_model, keep_alive="2m",
            dialogue_turns=dialogue_turns,
        )
    scene_reference = payload.get("scene_reference_image")
    image_instruction = build_multichar_image_instruction(
        char_a["name"], concept_a, char_b["name"], concept_b, scene_plan,
        char_a_picture=2 if scene_reference and not continuation_path else 1,
        char_b_picture=3 if scene_reference and not continuation_path else 2,
    )
    print(f"[worker] job {job['id']}: image instruction: {image_instruction[:400]}")

    h3_body = build_multichar_h3_video_prompt(
        char_a["name"], concept_a, char_b["name"], concept_b,
        enhanced_scene, interaction_text,
        spoken_line_a=spoken_line_a, spoken_line_b=spoken_line_b,
        dialogue_order=dialogue_order, dialogue_turns=dialogue_turns,
        scene_plan=scene_plan,
    )
    duration_seconds = multichar_video_duration(dialogue_turns, scene_plan)
    video_seed = random.randint(0, 2**31 - 1)
    completion_client.update_progress(
        phase="planned",
        prompt_template=performance_plan.VERSION,
        planner_status=scene_plan.get("planner_status", "unknown"),
        fallback_reason=scene_plan.get("fallback_reason", ""),
        scene_plan=scene_plan,
        image_prompt=image_instruction,
        h3_prompt=h3_body,
        duration_seconds=duration_seconds,
        video_seed=video_seed,
    )
    print(f"[worker] job {job['id']}: H3 prompt body (first 500): {h3_body[:500]}")
    # Planning is complete. Release Ollama before either GPU media model loads.
    evict_current_model()

    # Step 1: Generate the two-character scene image
    if continuation_path:
        completion_client.update_progress(phase="extracting_frame",
            continuation_of_artifact_id=payload.get("continuation_artifact_id"))
        final_frame = extract_video_continuation_frame(continuation_path)
        new_scene_filename = comfy_client.upload_bytes_to_input(final_frame, "multichar_continuation_frame.png")
        print(f"[worker] job {job['id']}: continuation frame -> {new_scene_filename}")
    else:
        if scene_reference:
            # Share image-job insertion, using only validated opening-frame
            # intent. Dialogue and later actions belong to the video stage.
            opening_scene = scene_plan.get("image_scene", scene_plan["visual_scene"])
            scene_image_filename = comfy_client.generate_two_character_scene_image(
                char_a["base_image"], char_b["base_image"], scene_reference,
                char_a["name"], char_b["name"], opening_scene,
                progress=completion_client.update_progress,
                latest_request=opening_scene,
                character_metadata={char_a["name"]: char_a, char_b["name"]: char_b},
            )
        else:
            scene_image_filename = comfy_client.generate_two_char_edit_image(
                char_a["base_image"], char_b["base_image"], image_instruction,
            )
        # Promote output to input so H3 can use it as scene reference
        new_scene_filename = comfy_client.promote_output_to_input(
            scene_image_filename["filename"], subfolder=scene_image_filename.get("subfolder", "")
        )
        print(f"[worker] job {job['id']}: 2-char scene image -> {new_scene_filename}")

    # Dwell (same anti-accumulation pause as single-char video)
    vps_delay = video_start_delay_seconds()
    if vps_delay > 0:
        print(f"[worker] job {job['id']}: holding {vps_delay:g}s before H3...")
        time.sleep(vps_delay)

    # Step 3: Generate the two-character video
    continuation_wf = None
    if continuation_path:
        continuation_wf = {
            "vertical": "presets/minimax_h3_2char_continue_vertical.json",
            "cinematic": "presets/minimax_h3_2char_continue_widescreen.json",
        }.get(get_aspect_mode(), "presets/minimax_h3_2char_continue_vertical.json")
    video_bytes = comfy_client.generate_multichar_voice_reference_video(
        char_a["base_image"], char_b["base_image"], new_scene_filename,
        char_a["voice_sample"], char_b["voice_sample"],
        h3_body, duration_seconds=duration_seconds, seed=video_seed,
        workflow_filename=continuation_wf,
        use_audio_references=bool(dialogue_turns),
        audio_reference_speakers=list(dict.fromkeys(
            turn["speaker"] for turn in dialogue_turns
        )),
        performance_plan=scene_plan,
    )
    video_b64 = base64.b64encode(video_bytes).decode("ascii")
    completion_client.post(f"{VPS_URL}/jobs/{job['id']}/complete",
                           json={"ok": True, "video_b64": video_b64})



def main():
    print(f"[worker] performance planner ready: {performance_plan.VERSION}", flush=True)
    global _active_settings
    completion_client = JobClient(VPS_URL, LOG_DIR / "completion-spool")
    print(f"[worker] polling {VPS_URL} every {POLL_SECONDS}s, model={OLLAMA_MODEL}")
    from worker.recovery import recover_videos
    next_recovery = 0
    while True:
        touch_heartbeat("worker")
        try:
            if not completion_client.drain():
                time.sleep(5)
                continue
            if time.monotonic() >= next_recovery:
                next_recovery = time.monotonic() + 30
                try:
                    recover_videos(completion_client, comfy_client.COMFY_URL)
                except requests.RequestException as exc:
                    print(f"[recovery] inspection deferred ({type(exc).__name__})")
                if not completion_client.drain():
                    continue
            resp = requests.get(f"{VPS_URL}/jobs/next", params={"worker_id": completion_client.worker_id}, timeout=10)
            resp.raise_for_status()
            job = resp.json()

            if job is None:
                time.sleep(POLL_SECONDS)
                continue

            _log_text = job["payload"].get("prompt") or job["payload"].get("motion_prompt", "")
            print(f"[worker] got job {job['id']} ({job['job_type']}): {_log_text[:60]!r}", flush=True)
            _active_settings = job["payload"].get("settings_snapshot") or {}
            #TRACK_ENTER_JOB {job["id"]}
            current_model = get_current_model()
            #TRACK_MODEL {current_model}
            try:
                configured_url = get_current_comfyui_url()
                if resource_identity(configured_url) != resource_identity(comfy_client.COMFY_URL):
                    raise SystemExit("ComfyUI endpoint changed; restart required to transfer resource lock")
                comfy_client.COMFY_URL = configured_url
            except Exception:
                pass
            #TRACK_COMFY {comfy_client.COMFY_URL}
            try:
                comfy_client.set_content_mode(get_content_mode())
            except Exception:
                pass
            #TRACK_MODE {get_content_mode()}
            try:
                comfy_client.set_aspect_mode(get_aspect_mode())
            except Exception:
                pass
            #TRACK_ASPECT {get_aspect_mode()}
            try:
                completion_client.begin(job)
                completion_client.update_progress(phase="starting")
                comfy_client.PROGRESS_CALLBACK = completion_client.record_prompt
                if job["job_type"] in ("character_image_edit", "voice_sample_batch", "multi_character_image", "multi_character_video", "comfy_image_batch"):
                    evict_current_model(current_model)
                if job["job_type"] == "list_models":
                    resp = requests.get(f"{get_current_ollama_url()}/api/tags", timeout=15)
                    resp.raise_for_status()
                    model_names = [m["name"] for m in resp.json().get("models", [])]
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "models": model_names},
                        timeout=15,
                    )

                elif job["job_type"] == "promote_scene_anchor":
                    source = requests.get(f"{VPS_URL}{job['payload']['artifact_url']}", timeout=30)
                    source.raise_for_status()
                    owner = job["payload"].get("scene_id") or job["payload"].get("character_id")
                    aspect = job["payload"].get("aspect_mode", "legacy")
                    filename = comfy_client.upload_image_bytes_to_input(
                        source.content, f"scene_anchor_{owner}_{aspect}_{job['payload']['artifact_id']}.png")
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok":True,"text":filename},timeout=30,
                    )

                elif job["job_type"] == "comfy_image_batch":
                    image_bytes = comfy_client.generate_image(job["payload"]["prompt"])
                    image_b64 = base64.b64encode(image_bytes).decode("ascii")
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "image_b64": image_b64},
                        timeout=30,
                    )

                elif job["job_type"] == "character_image_batch":
                    # "Generating look candidates" step — switchable via /workflow.
                    look_strategy = get_current_workflow("look")
                    look_wf = ("k2_txt2img_api.json" if not look_strategy
                               else look_strategy if look_strategy.endswith(".json")
                               else "k2_txt2img_api.json")
                    print(f"[worker] job {job['id']}: look workflow={look_wf}")
                    # LLM-driven LOOK candidates: ask the active model for `n`
                    # DIFFERENT people who all fit the concept (not `n` seeds of the
                    # same person), then render each with krea2's own composition.
                    n_look = 3
                    concept = job["payload"].get("prompt", "")
                    (variant_descs, stylized) = generate_look_variants_llm(concept, n=n_look, personality_notes=job["payload"].get("personality_notes", ""))
                    # Free the LLM before ComfyUI loads krea2 — mirrors the video
                    # path's eviction discipline; otherwise both models compete
                    # for the 16GB card (qwen3 can sit resident via keep_alive).
                    evict_current_model()
                    results = comfy_client.generate_look_variants(
                        variant_descs, photoreal=not stylized, workflow_filename=look_wf,
                    )
                    # Partial-batch contract: failed candidates come back as a
                    # single ("__ERRORS__", note) sentinel — strip it, deliver
                    # the survivors, and surface the failures in the job text.
                    batch_errors = next(
                        (n for b, n in results if b == "__ERRORS__"), None)
                    real = [(b, fname) for b, fname in results if b != "__ERRORS__"]
                    if not real:
                        raise RuntimeError(batch_errors or "no look candidates produced")
                    images_b64 = [base64.b64encode(b).decode("ascii") for b, _ in real]
                    filenames = [fname for _, fname in real]
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "images_b64": images_b64,
                              "filenames": filenames,
                              **({"text": f"Some candidates failed: {batch_errors}"}
                                 if batch_errors else {})},
                        timeout=180,  # sequential Telegram photo uploads on the VPS side can be slow
                    )

                elif job["job_type"] == "promote_reference":
                    new_filename = comfy_client.promote_output_to_input(job["payload"]["filename"])
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "text": new_filename},
                        timeout=30,
                    )

                elif job["job_type"] == "voice_sample_batch":
                    payload = job["payload"]
                    gender_hint = payload.get("gender_hint", "female")
                    # Optional pre-made delivery: if the payload lists local audio
                    # files (absolute paths) under "premade_files", deliver those
                    # exactly as-is through the normal complete_job path instead of
                    # generating new candidates. Used for MOSS/offline voice batches
                    # generated on a different ComfyUI instance (e.g. port 8189).
                    if payload.get("premade_files"):
                        audios_b64, audio_files = [], []
                        for j, p in enumerate(payload["premade_files"], start=1):
                            with open(p, "rb") as f:
                                audios_b64.append(
                                    base64.b64encode(f.read()).decode("ascii"))
                            audio_files.append({
                                "filename": os.path.basename(p),
                                "subfolder": "audio",
                                "speaker": payload.get("premade_labels", ["moss-%d" % j])[j-1],
                            })
                        print(f"[worker] job {job['id']}: delivering {len(audios_b64)} premade voice files")
                        completion_client.post(
                            f"{VPS_URL}/jobs/{job['id']}/complete",
                            json={"ok": True, "audios_b64": audios_b64,
                                  "audio_files": audio_files}, timeout=180,
                        )
                        continue
                    voice_strategy = get_current_workflow("voice") or "moss"
                    print(f"[worker] job {job['id']}: voice workflow={voice_strategy}")
                    # Only the MOSS engine ships now (retired F5/Kokoro/Qwen3/Chatterbox).
                    # If a stale strategy name is somehow set, fall back safely to MOSS.
                    if voice_strategy != "moss":
                        print(f"[worker] job {job['id']}: unknown voice strategy '{voice_strategy}' — using MOSS")
                    raw = comfy_client.generate_voice_candidates_moss(
                        payload["prompt"], n=3, gender_hint=gender_hint,
                        voice_description=payload.get("voice_description", ""),
                    )
                    # Partial-batch contract: failed candidates arrive as one
                    # (b"__ERRORS__", "", "", note) sentinel — strip, deliver
                    # survivors, surface failures in the completion text.
                    batch_errors = next(
                        (note for b, _, _, note in raw if b == b"__ERRORS__"), None)
                    real = [t for t in raw if t[0] != b"__ERRORS__"]
                    if not real:
                        raise RuntimeError(batch_errors or "no voice candidates produced")
                    audios_b64 = [base64.b64encode(b).decode("ascii") for b, _, _, _ in real]
                    # Each entry records the real filename + subfolder (for promote)
                    # plus the reference-voice label so we can caption which voice it is.
                    audio_files = [
                        {"filename": fname, "subfolder": sub, "speaker": spk}
                        for _, fname, sub, spk in real
                    ]
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "audios_b64": audios_b64, "audio_files": audio_files,
                              **({"text": f"Some voice candidates failed: {batch_errors}"}
                                 if batch_errors else {})},
                        timeout=180,  # 3 audio uploads + the 3 Telegram sends can be slow
                    )

                elif job["job_type"] == "promote_voice":
                    filename = job["payload"]["filename"]
                    # Keep the selected performance; only remove its greeting.
                    new_filename = comfy_client.promote_selected_voice(
                        filename, job["payload"].get("subfolder", "")
                    )
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "text": new_filename},
                        timeout=30,
                    )

                elif job["job_type"] == "character_image_edit":
                    # "Generating images (edit)" step — workflow switchable via /workflow.
                    # Curated keys map to their known file; anything else is treated
                    # as a workflow filename dropped into the workflows/ folder.
                    img_strategy = get_current_workflow("image")
                    # Mode-aware image default: an explicit .json workflow set via
                    # Only compatible native identity-edit overrides are honored; otherwise the
                    # Default is the native identity-edit workflow selected
                    # by content x aspect, with SNOFS enabled only in NSFW mode.
                    payload = job["payload"]
                    has_scene = bool(payload.get("scene_reference_image"))
                    edit_wf = comfy_client.resolve_identity_workflow(
                        img_strategy if not has_scene and img_strategy and img_strategy.endswith(".json") else None)
                    instruction = build_image_prompt(
                        payload["prompt"], ((2 if has_scene else 1, "the character"),),
                        1 if has_scene else None, latest_request=payload.get("latest_request"),
                        adult_mode=get_content_mode() == 'nsfw')
                    completion_client.update_progress(phase="rendering_image", image_prompt=instruction,
                        image_workflow=edit_wf)
                    print(f"[worker] job {job['id']}: identity workflow={edit_wf}, prompt={instruction}")
                    if has_scene:
                        image_bytes = comfy_client.generate_character_scene_image(
                            payload["reference_image"], payload["scene_reference_image"], instruction)
                    else:
                        image_bytes = comfy_client.generate_edit_image(
                            payload["reference_image"], instruction, workflow_filename=edit_wf)
                    image_b64 = base64.b64encode(image_bytes).decode("ascii")
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "image_b64": image_b64},
                        timeout=30,
                    )

                elif job["job_type"] == "character_video":
                    payload = job["payload"]
                    continuation_path = payload.get("continuation_relative_path")
                    print(f"[worker] job {job['id']}: " + ("continuing prior video..." if continuation_path else "generating scene image first..."))
                    # Preserve the original request across image, audio and motion planning.
                    raw_scene = payload.get("motion_prompt") or payload.get("scene_prompt") or ""
                    # The spoken line is the SHORT in-character UTTERANCE she says in
                    # the clip. It must NEVER be the caption: the caption is descriptive
                    # prose ABOUT the video (what the user reads by the media thumbnail),
                    # and feeding it verbatim as dialogue is exactly what makes MiniMax
                    # H3 recite the caption (job 714/716: spoken_line == caption).
                    spoken_line = payload.get("spoken_line") or payload.get("video_line")
                    if payload.get("spoken_line") is None and payload.get("video_line") is None:
                        # No distinct line drafted by the classifier
                        extracted, spoken_line = extract_spoken_line(raw_scene)
                        # Keep source wording and speech position for the video contract.
                        # Extraction only discovers a missing payload line.
                    # NEVER let a descriptive caption become the spoken line.
                    if spoken_line and payload.get("caption") and \
                       spoken_line.strip().lower() == payload.get("caption").strip().lower():
                        spoken_line = None
                    print(f"[worker] job {job['id']}: " + (
                        "preserving literal continuation request..." if continuation_path
                        else "enhancing scene into literal description..."
                    ))
                    # Single-video prompt compilation does not load an LLM.
                    if continuation_path:
                        enhanced_scene = continuation_scene_instruction(raw_scene)
                    else:
                        enhanced_scene = enhance_scene(
                            raw_scene, payload.get("character_name", "the character"),
                            spoken_line=spoken_line)
                    image_instruction = build_image_prompt(
                        enhanced_scene,
                        identities=((2 if payload.get("scene_reference_image") else 1, "the character"),),
                        scene_index=1 if payload.get("scene_reference_image") else None,
                        latest_request=payload.get("latest_request"), adult_mode=get_content_mode() == 'nsfw',
                    )
                    # --- Determine the video engine + build the H3 prompt NOW (both are
                    # --- pure functions of payload fields, independent of the rendered image),
                    # --- so all LLM work happens up front and the GPU pipeline
                    # --- (image -> video) runs uninterrupted below. ---
                    voice_audio = payload.get("voice_audio")
                    video_strategy = get_current_workflow("video") or "h3_ref2v"
                    print(f"[worker] job {job['id']}: video workflow={video_strategy}")
                    # The H3 video workflow is chosen by aspect mode
                    # only (vertical -> 9:16 ref2v, cinematic -> 16:9 ref2v). Both are
                    # MiniMaxH3ReferenceToVideo. The reference image aspect (chosen
                    # below) always mirrors this H3 resolution node.
                    _NEWDEFAULTS_H3 = {
                        "vertical": "presets/minimax_h3_ref2v_vertical.json",
                        "cinematic": "presets/minimax_h3_ref2v_widescreen.json",
                    }
                    video_wf_file = _NEWDEFAULTS_H3.get(
                        get_aspect_mode(),
                        "presets/minimax_h3_ref2v_vertical.json",
                    )
                    if continuation_path:
                        video_wf_file = {
                            "vertical": "presets/minimax_h3_continue_vertical.json",
                            "cinematic": "presets/minimax_h3_continue_widescreen.json",
                        }.get(get_aspect_mode(), "presets/minimax_h3_continue_vertical.json")
                    use_ref2v = bool(voice_audio)
                    # spoken_line is already resolved at the top of this handler:
                    # it may come from the payload (chat-intent drafted line /
                    # caption) OR from extract_spoken_line (quoted dialogue pulled
                    # out of the user's description). Do NOT re-derive it here —
                    # that would drop an extracted line back to None. Only pass it
                    # through to the H3 prompt when a voice reference is present.
                    if not use_ref2v:
                        spoken_line = None
                    # Both stages use the immutable original request; H3 compiles exact audio.
                    if continuation_path:
                        print(f"[worker] job {job['id']}: building deterministic continuation prompt...")
                        h3_body = build_h3_continuation_prompt(
                            raw_scene,
                            payload.get("character_name", "the character"),
                            spoken_line=spoken_line if use_ref2v else None,
                            has_audio=use_ref2v,
                        )
                    else:
                        print(f"[worker] job {job['id']}: compiling source timeline into H3 format...")
                        h3_body = build_h3_video_prompt(
                            raw_scene,
                            payload.get("character_name", "the character"),
                            spoken_line=spoken_line if use_ref2v else None,
                            has_audio=use_ref2v,
                            scene_text=raw_scene,
                        )
                    # Release any resident chat model before media, so it does not fight
                    # them for VRAM (GPU crash). Evict = keep_alive 0 = unload now.
                    evict_current_model()
                    # --- Now the GPU pipeline: generate the scene image, then animate it. ---
                    # Scene image uses the selected k2/clownshark reference workflow,
                    # chosen by CONTENT x ASPECT. Each file already bakes in the right
                    # model (vanilla vs gonzoloma), the right LoRAs (filterbypass only
                    # in NSFW), and the right aspect (3:4 vertical vs 16:9 widescreen),
                    # and it uses the character's identity image as reference.
                    if continuation_path:
                        completion_client.update_progress(phase="extracting_frame",
                            continuation_of_artifact_id=payload.get("continuation_artifact_id"))
                        scene_image_bytes = extract_video_continuation_frame(continuation_path)
                        scene_filename = comfy_client.upload_bytes_to_input(scene_image_bytes, "video_continuation_frame.png")
                    else:
                        scene_edit_wf = _NEWDEFAULTS_REF[(get_aspect_mode(), get_content_mode())]
                        if payload.get("scene_reference_image"):
                            scene_image_bytes = comfy_client.generate_character_scene_image(
                                payload["reference_image"],payload["scene_reference_image"],image_instruction,
                                apply_aspect=False,
                            )
                        else:
                            scene_image_bytes = comfy_client.generate_edit_image(
                                payload["reference_image"], image_instruction,
                                workflow_filename=scene_edit_wf,
                                # The preset file already bakes in the correct aspect per
                                # config, so never override it with /aspect here.
                                apply_aspect=False,
                            )
                        scene_filename = comfy_client.upload_bytes_to_input(scene_image_bytes, "video_scene.png")
                    guided_speech_audio = None
                    if use_ref2v:
                        audio_contract = VideoRequest.create(
                            raw_scene, payload.get("character_name", "the character"), spoken_line)
                        guide_words = " ".join(audio_contract.spoken_words())
                        if guide_words:
                            completion_client.update_progress(phase="rendering_voice")
                            print(f"[worker] job {job['id']}: synthesizing exact video dialogue...")
                            guided_speech_audio = comfy_client.generate_moss_line_from_saved_voice(
                                voice_audio, guide_words,
                            )
                            print(f"[worker] job {job['id']}: requested dialogue -> {guided_speech_audio}")
                    # Anti-accumulation dwell: give ComfyUI time to fully release the
                    # Krea2 ref-render working set before the MiniMax H3 stack loads.
                    # Keep a short configurable pause for asynchronous ComfyUI cleanup.
                    _delay = video_start_delay_seconds()
                    if _delay > 0:
                        print(f"[worker] job {job['id']}: holding {_delay:.0f}s before H3 (let Krea2 unload and async-offload settle)...", flush=True)
                        time.sleep(_delay)
                    print(f"[worker] job {job['id']}: scene ready ({scene_filename}), animating...")
                    if use_ref2v:
                        # Reference-to-Video: the clip speaks in the character's voice.
                        print(f"[worker] job {job['id']}: using voice reference {voice_audio}")
                        video_bytes = comfy_client.generate_voice_reference_video(
                            scene_filename, voice_audio,
                            h3_prompt=h3_body,
                            duration_seconds=payload.get("duration", 5.0),
                            workflow_filename=video_wf_file,
                            guided_speech_audio=guided_speech_audio,
                        )
                    else:
                        video_bytes = comfy_client.generate_video(
                            scene_filename, h3_body,
                            duration_seconds=payload.get("duration", 6.0),
                            workflow_filename=video_wf_file,
                        )
                    video_b64 = base64.b64encode(video_bytes).decode("ascii")
                    completion_client.post(f"{VPS_URL}/jobs/{job['id']}/complete",
                                           json={"ok": True, "video_b64": video_b64})

                elif job["job_type"] == "character_chat_intent":
                    from worker.conversation import respond
                    result = respond(job["payload"], current_model, call_ollama,
                                     evict_current_model, get_content_mode(),
                                     get_character_videos_enabled())
                    completion_client.post(f"{VPS_URL}/jobs/{job['id']}/complete", json=result)

                elif job["job_type"] == "multi_character_image":
                    payload = job["payload"]
                    char_a = payload["char_a"]
                    char_b = payload["char_b"]
                    raw_scene = normalize_multichar_dialogue_quotes(
                        payload.get("scene_text") or ""
                    )
                    visual_scene = _multichar_visual_scene(
                        raw_scene, char_a["name"], char_b["name"]
                    ).strip() or raw_scene
                    scene_reference = payload.get("scene_reference_image")
                    if not scene_reference:
                        image_instruction = build_multichar_image_instruction(
                            char_a["name"], char_a.get("concept") or "",
                            char_b["name"], char_b.get("concept") or "",
                            {"visual_scene": visual_scene, "turns": []},
                            latest_request=payload.get("latest_request") or raw_scene,
                        )
                        completion_client.update_progress(
                            phase="rendering_image", image_prompt=image_instruction,
                        )
                    print(
                        f"[worker] job {job['id']}: two-character image "
                        f"({char_a['name']} + {char_b['name']})"
                    )
                    evict_current_model()
                    selected_image_workflow = get_current_workflow("image")
                    image_override = (selected_image_workflow if selected_image_workflow
                                      and selected_image_workflow.endswith(".json") else None)
                    if scene_reference:
                        if image_override:
                            print("[worker] sequential scene insertion uses the native identity-edit baseline; "
                                  "image override applies only to images without a scene reference")
                        image_info = comfy_client.generate_two_character_scene_image(
                            char_a["base_image"], char_b["base_image"], scene_reference,
                            char_a["name"], char_b["name"], visual_scene,
                            progress=completion_client.update_progress,
                            latest_request=payload.get("latest_request") or raw_scene,
                            character_metadata={char_a['name']: char_a, char_b['name']: char_b},
                        )
                    else:
                        image_info = comfy_client.generate_two_char_edit_image(
                            char_a["base_image"], char_b["base_image"], image_instruction,
                            workflow_filename=comfy_client.resolve_identity_workflow(image_override),
                        )
                    image_bytes = comfy_client.fetch_image_bytes(image_info)
                    image_b64 = base64.b64encode(image_bytes).decode("ascii")
                    completion_client.post(
                        f"{VPS_URL}/jobs/{job['id']}/complete",
                        json={"ok": True, "image_b64": image_b64}, timeout=60,
                    )

                elif job["job_type"] == "multi_character_video":
                    process_multichar_video(job, completion_client, current_model)

                elif job["job_type"] == "edit_profile_generate":
                    text = call_ollama(build_profile_prompt(job["payload"]["personality_notes"]), model=current_model)
                    completion_client.post(f"{VPS_URL}/jobs/{job['id']}/complete", json={"ok": True, "text": text}, timeout=10)

                elif job["job_type"] == "profile_generate":
                    notes = job["payload"]["personality_notes"]
                    text = call_ollama(build_profile_prompt(notes), model=current_model)
                    completion_client.post(f"{VPS_URL}/jobs/{job['id']}/complete", json={"ok": True, "text": text}, timeout=10)

                else:  # llm_chat (default)
                    payload = job["payload"]
                    history = payload.get("history") or ""
                    if history:
                        prompt = (
                            f"Recent conversation (so far):\n{history}\n\n"
                            f"Continue the conversation naturally. The user just said:\n"
                            f"{payload['prompt']}"
                        )
                    else:
                        prompt = payload["prompt"]
                    text = call_ollama(prompt, model=current_model)
                    completion_client.post(f"{VPS_URL}/jobs/{job['id']}/complete", json={"ok": True, "text": text}, timeout=10)
                print(f"[worker] job {job['id']} done")
            except CompletionPending as e:
                print(f"[worker] job {job['id']}: {e}", flush=True)
            except Exception as e:
                completion_client.post(
                    f"{VPS_URL}/jobs/{job['id']}/complete",
                    json={"ok": False, "text": f"{type(e).__name__}: {e}"},
                    timeout=10,
                )
                print(f"[worker] job {job['id']} failed: {e}")
            finally:
                comfy_client.PROGRESS_CALLBACK = None
                completion_client.end()

        except requests.exceptions.RequestException as e:
            print(f"[worker] VPS unreachable: {e} — retrying in 3s")
            time.sleep(3)
        except Exception as e:
            # Catch-all so ONE bad job envelope / malformed VPS response can't
            # kill the loop (start.py has no supervisor — a dead worker stays
            # dead silently). Log loudly, back off, keep polling.
            print(f"[worker] LOOP ERROR ({type(e).__name__}: {e}) — continuing in 5s")
            time.sleep(5)


if __name__ == "__main__":
    comfy_client.COMFY_URL = get_current_comfyui_url()
    _instance = InstanceLock(resource_identity(comfy_client.COMFY_URL))
    if not _instance.acquire():
        raise SystemExit("Another worker owns this ComfyUI endpoint")
    _heartbeat = Heartbeat("worker").start()
    try:
        main()
    finally:
        _heartbeat.close()
        _instance.close()
