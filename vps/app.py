"""
VPS side of the minimal slice.

Two jobs, running together in one process:
  1. Long-poll Telegram for new messages -> write a `jobs` row -> send an
     immediate ack back to the chat ("On it...").
  2. A tiny HTTP API (Flask) that the worker calls over a trusted network
     to claim the next queued job and post back its result. When a result
     comes in, push it to the original Telegram chat.

(Historical note: the original minimal slice had no scheduler, peak-hour
gate, or character state machine — all three have since shipped.)

Env vars required:
  TELEGRAM_BOT_TOKEN   - from @BotFather
  DB_PATH              - defaults to ./jobs.db
  API_PORT             - defaults to 8787 (local-only unless API_HOST is configured;
                          intended for trusted networks)
"""

import os
import glob
import json
import logging
import sqlite3
import threading
import time
import base64
import io
import math
import random
import re
import sys
import traceback
import uuid
import faulthandler
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

# During direct startup, expose a stack trace if imports or initialization hang
# before the regular heartbeat can start. Cancel this timer before serving.
if __name__ == "__main__":
    faulthandler.dump_traceback_later(45, repeat=False, file=sys.stderr)

import requests
from flask import Flask, request, jsonify

from PIL import Image, ImageDraw, ImageFont

# Heartbeat file helpers -----------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.runtime import Heartbeat, InstanceLock, resource_identity
from vps.delivery import DeliveryWorker, enqueue as _defer
from vps.context_migration import migrate_contexts
from vps.artifacts import persist_artifact
from vps.scenes import active_scene_view, scene_view
from worker.image_prompts import build_image_prompt, compose_chat_media_scene


def touch_heartbeat(name):
    # Compatibility for existing loop calls. Liveness is maintained independently.
    pass

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "1" if TELEGRAM_BOT_TOKEN else "0") == "1"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "jobs.db"))
API_PORT = int(os.environ.get("API_PORT", "8787"))
# Default endpoint IPs for Ollama and ComfyUI. Stored in the `settings` table
# under `endpoint_ollama` / `endpoint_comfyui` once a user changes them via
# /endpoints; these defaults are the fallback if never overridden.
DEFAULT_ENDPOINTS = {
    "ollama": "http://127.0.0.1:11434",
    "comfyui": "http://127.0.0.1:8188",
}

# Global content mode: 'sfw' (default) or 'nsfw'. When SFW, the worker strips
# all LoRAs from image workflows and guides every generated prompt (scene, image,
# H3 video, chat) away from nudity/explicit sexual content — but does NOT resist
# or censor an explicit request the user types directly (passive moderation).
DEFAULT_CONTENT_MODE = "sfw"

# Global aspect mode: 'vertical' (default) or 'cinematic'. Orthogonal to the
# SFW/NSFW content mode. When 'cinematic', image + video generation switches to
# widescreen (16:9) dimensions and a cinematic rendering of the prompt recipe.
DEFAULT_ASPECT_MODE = "vertical"

app = Flask(__name__)
db_lock = threading.Lock()  # SQLite + threads: keep it simple and serialize access

# Rotating file logging for this long-running process. The long-poll listener is
# chatty; without rotation vps.log grew to ~19MB in two weeks of personal use.
# IMPORTANT: start.py ALSO opens `logs/vps.log` as this process's stdout/stderr
# (Popen stdout=out,stderr=out). Two handles on the SAME file make Windows
# reject the RotatingFileHandler's os.rename(vps.log->vps.log.1) with WinError 32
# on every rotation (a PermissionError storm that thrashes the VPS). So the
# internal root-logger handler writes to its OWN file (vps-internal.log);
# start.py's stdio capture continues to land in vps.log. Decoupled -> rotation works.
from logging.handlers import RotatingFileHandler
_LOG_DIR = os.environ.get("CC_LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"))
os.makedirs(_LOG_DIR, exist_ok=True)
_rot = RotatingFileHandler(os.path.join(_LOG_DIR, "vps-internal.log"),
                           maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
_rot.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_rot)
logging.getLogger().setLevel(logging.INFO)

TZ = ZoneInfo("America/Los_Angeles")
PEAK_START = dtime(16, 0)
PEAK_END = dtime(21, 0)
# --- Peak-hours gate ----------------------------------------------------------
# The gate (heavy jobs blocked 16:00–21:00 PT) is OFF by default. It shipped ON
# and silently queued ALL heavy jobs — including every character-creation step —
# for hours with no error or ETA (the worst possible first-run experience).
# Operators who WANT evening back-off can opt in via PEAK_HOURS_GATE=1.
_PEAK_GATE_ON = os.environ.get("PEAK_HOURS_GATE", "0").lower() in ("1", "on", "true", "yes")
# -----------------------------------------------------------------------------


def is_peak_hours() -> bool:
    """DST-aware peak-hour check. Set FAKE_NOW (e.g. '2026-07-31T18:30:00')
    to test without touching the system clock — never set in production.
    The peak-hours gate is OFF by default; opt in via PEAK_HOURS_GATE=1
    (blocks heavy jobs during the window, light jobs always run)."""
    if not _PEAK_GATE_ON:
        return False
    override = os.environ.get("FAKE_NOW")
    now = datetime.fromisoformat(override) if override else datetime.now(TZ)
    return PEAK_START <= now.time() < PEAK_END


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn, open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
        _migrate_messages_nullable(conn)
        _migrate_deliveries_nullable(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, kind in {"lease_token": "TEXT", "lease_expires_at": "REAL",
                           "worker_id": "TEXT", "progress_json": "TEXT"}.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        migrate_contexts(conn)
        scene_columns = {row["name"] for row in conn.execute("PRAGMA table_info(character_scenes)")}
        if "anchor_artifact_id" not in scene_columns:
            conn.execute("ALTER TABLE character_scenes ADD COLUMN anchor_artifact_id INTEGER")
        if "anchor_reference_image" not in scene_columns:
            conn.execute("ALTER TABLE character_scenes ADD COLUMN anchor_reference_image TEXT")
        from vps.scenes import migrate_reusable_scenes
        migrate_reusable_scenes(conn)


def _migrate_deliveries_nullable(conn):
    columns = conn.execute("PRAGMA table_info(deliveries)").fetchall()
    if not any(row["name"] == "job_id" and row["notnull"] for row in columns):
        return
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='deliveries'").fetchone()[0]
    sql = sql.replace("job_id INTEGER NOT NULL", "job_id INTEGER")
    conn.execute("ALTER TABLE deliveries RENAME TO deliveries_old")
    conn.execute(sql)
    conn.execute("INSERT INTO deliveries SELECT * FROM deliveries_old")
    conn.execute("DROP TABLE deliveries_old")
    conn.execute("CREATE INDEX idx_deliveries_pending ON deliveries(status,next_attempt)")


def _migrate_messages_nullable(conn):
    """Idempotent migration: the original `messages` table declared
    `character_id INTEGER NOT NULL`, which blocked the bare llm_chat fallback
    (no character). Rebuild the table allowing NULL (SQLite lacks ALTER COLUMN).
    No-op once the column is already nullable."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages'"
    ).fetchone()
    if not row:
        return
    if "character_id INTEGER NOT NULL" in (row["sql"] or ""):
        # rebuild: new table -> copy -> drop -> rename; recreate indexes after.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            CREATE TABLE messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                kind TEXT,
                content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (character_id) REFERENCES characters(id)
            );
            INSERT INTO messages_new (id, character_id, chat_id, role, kind, content, created_at)
                SELECT id, character_id, chat_id, role, kind, content, created_at FROM messages;
            DROP TABLE messages;
            ALTER TABLE messages_new RENAME TO messages;
            CREATE INDEX IF NOT EXISTS idx_messages_char ON messages(character_id, chat_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_fallback ON messages(chat_id, created_at);
            """
        )
        conn.execute("PRAGMA foreign_keys=ON")


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# Catalog of switchable generation workflows, mirroring how /model switches the
# LLM. Each step (key) has a list of "strategies" — compatible generation paths
# for that step. The selection is stored in settings as `workflow_<step>` and
# read by the worker when it routes each job. "default" is the fallback.
#
# The catalog is BUILT from two sources:
#   1. KNOWN_ENGINES — the curated built-in strategies (human labels + special
#      worker routing for engine-specific steps like voice/video).
#   2. A scan of the workflows/ folder — any *.json workflow file you drop in is
#      auto-classified into a step (by its node types) and offered as a
#      switchable option, keyed by its filename. So adding a new workflow file
#      automatically makes it selectable without code changes.
CURATED_STRATEGIES = {
    "look": [
        {"key": "k2_txt2img", "label": "Krea2 FP8 txt2img (default)"},
    ],
    "voice": [
        {"key": "moss", "label": "MOSS VoiceGenerator (default)"},
    ],
    "image": [
        {"key": "k2_ref", "label": "Krea2 identity edit (mode-aware default)"},
        {"key": "k2_ref_sfw", "label": "Krea2 identity edit (SFW default)"},
    ],
    "video": [
        {"key": "presets/minimax_h3_ref2v_vertical.json", "label": "MiniMax H3 ReferenceToVideo (Vertical, 9:16)"},
        {"key": "presets/minimax_h3_ref2v_widescreen.json", "label": "MiniMax H3 ReferenceToVideo (Widescreen, 16:9)"},
    ],
}
WORKFLOW_STEP_META = [
    {"key": "look", "label": "Generating look candidates", "default": "k2_txt2img"},
    {"key": "voice", "label": "Generating voice candidates", "default": "moss"},
    {"key": "image", "label": "Generating images", "default": "k2_ref"},
    {"key": "video", "label": "Generating video", "default": "h3_ref2v"},
]
# Worker workflow files live one directory up from this vps/ folder.
WORKFLOWS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker", "workflows")

# Known workflow files that are already the backend of a curated engine, so the
# auto-scan does not list them again as generic filename strategies.
EXCLUDE_FROM_SCAN = {
    "k2_txt2img_api.json",        # backend of curated "k2_txt2img"
    "krea2_identity_edit_clownshark.json",  # shared mode/aspect image baseline
}

def _infer_workflow_mode(workflow_key: str) -> str | None:
    """Return 'nsfw' or 'sfw' based on the workflow file name heuristic, or
    None if the workflow is ambiguous (not an image workflow, or doesn't carry
    a mode signal). Files with '_17gb', '_nsfw', or 'SNOFS' in the name are NSFW;
    all other explicit-image workflows are SFW. Pass through curated engine keys
    that are known SFW (k2_ref_sfw) vs NSFW (k2_ref = mode-aware default).
    """
    lower = workflow_key.lower()
    if lower in ("krea2_identity_edit_snofs_unload.json", "krea2_identity_edit_clownshark.json"):
        return None  # One tested template serves both modes via LoRA strength.
    if any(marker in lower for marker in ("_17gb", "_nsfw", "snofs")):
        return "nsfw"
    if workflow_key in ("k2_ref", "k2_txt2img"):
        return "nsfw"
    if workflow_key in ("k2_ref_sfw",):
        return "sfw"
    # Generic *.json found by auto-scan — assume SFW unless name says NSFW
    if lower.endswith(".json"):
        return "sfw"
    return None


def _image_default_for_mode(mode: str) -> str:
    """Return the default curated image workflow key for the given content mode.
    This is what the worker's _NEWDEFAULTS_REF would select for vertical aspect,
    but as a VPS-side setting key so the workflow setting isn't stranded on an
    aspect-specific file after a mode switch.
    """
    if mode == "sfw":
        return "k2_ref_sfw"
    return "k2_ref"  # NSFW default = the gonzo-curated engine



def _classify_workflow_file(path: str) -> str | None:
    """Inspect a workflow JSON's node class_types to decide which step it
    belongs to, or None if it can't be classified. Matches how the worker's
    generation functions expect that step's node schema to look."""
    try:
        with open(path) as f:
            wf = json.load(f)
    except (OSError, ValueError):
        return None
    class_types = {v.get("class_type", "") for v in wf.values()}
    if any("ReferenceToVideo" in c or "ImageToVideo" in c for c in class_types):
        return "video"
    if any("F5" in c or "Kokoro" in c or "Chatterbox" in c or "Zonos" in c or c == "TTSLoader" or "TTS" in c for c in class_types):
        return "voice"
    if any("Edit" in c or "FluxKontext" in c or "TextEncodeQwenImageEditPlus" in c for c in class_types):
        return "image"
    if "SaveImage" in class_types:
        return "look"
    return None


_STEPS_CACHE = {"sig": None, "steps": None}


def _workflows_signature() -> tuple:
    """Cheap change signature for the workflows folder: the directory mtime
    plus every *.json filename+mtime, sorted. Detects dropped-in files, edits
    to ANY existing file (not just the newest — a plain max-mtime check misses
    those), and deletions."""
    try:
        sig = [os.path.getmtime(WORKFLOWS_DIR)]
        for f in glob.glob(os.path.join(WORKFLOWS_DIR, "*.json")):
            sig.append((os.path.basename(f), os.path.getmtime(f)))
        sig.sort(key=lambda x: str(x))
        return tuple(sig)
    except OSError:
        return ()


def build_workflow_steps():
    """Assemble the full, current catalog: curated engines for each step, plus
    any workflow *.json files found in the workflows/ folder (auto-classified
    into a step, keyed by filename). Cached; the cache is invalidated whenever
    the workflows directory's mtime changes, so newly-dropped workflow files
    still show up without a restart — but repeated calls within one request
    no longer re-glob + re-parse every JSON on disk."""
    if (_STEPS_CACHE["steps"] is not None
            and _STEPS_CACHE["sig"] == _workflows_signature()):
        return _STEPS_CACHE["steps"]
    steps = []
    for meta in WORKFLOW_STEP_META:
        step = dict(meta)
        step["strategies"] = list(CURATED_STRATEGIES[meta["key"]])
        # Avoid duplicate keys if a scanned file shares a curated key.
        curated_keys = {s["key"] for s in step["strategies"]}
        for fname in sorted(glob.glob(os.path.join(WORKFLOWS_DIR, "*.json"))):
            fbase = os.path.basename(fname)
            if fbase in curated_keys or fbase in EXCLUDE_FROM_SCAN:
                continue
            target_step = _classify_workflow_file(fname)
            if target_step == step["key"]:
                step["strategies"].append({"key": fbase, "label": fbase})
        steps.append(step)
    _STEPS_CACHE["sig"] = _workflows_signature()
    _STEPS_CACHE["steps"] = steps
    return steps


def workflow_step_by_key(key: str):
    """Return the catalog step entry for a given step key, or None."""
    return next((s for s in build_workflow_steps() if s["key"] == key), None)


def get_workflow_strategy(conn, step_key: str):
    """Return the currently-selected strategy dict for a step, falling back to
    the step's default strategy."""
    step = workflow_step_by_key(step_key)
    if not step:
        return None
    selected = get_setting(conn, f"workflow_{step_key}")
    if selected is None:
        selected = (_image_default_for_mode(
            get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE
        ) if step_key == "image" else step["default"])
    return next((s for s in step["strategies"] if s["key"] == selected), step["strategies"][0])


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "pid": os.getpid()})


@app.route("/settings/model", methods=["GET"])
def get_model_setting():
    with db_lock, get_db() as conn:
        return jsonify({"model": get_setting(conn, "current_model")})


@app.route("/settings/endpoints", methods=["GET"])
def get_endpoints_setting():
    """Return the current Ollama/ComfyUI base URLs, for the worker to read
    per-job (mirrors /settings/model). Falls back to DEFAULT_ENDPOINTS when a
    key hasn't been overridden yet."""
    with db_lock, get_db() as conn:
        return jsonify({
            "ollama": get_setting(conn, "endpoint_ollama") or DEFAULT_ENDPOINTS["ollama"],
            "comfyui": get_setting(conn, "endpoint_comfyui") or DEFAULT_ENDPOINTS["comfyui"],
        })


@app.route("/settings/content_mode", methods=["GET"])
def get_content_mode_setting():
    """Return the current global content mode ('nsfw' | 'sfw') for the worker to
    read per-job. Falls back to DEFAULT_CONTENT_MODE when never overridden."""
    with db_lock, get_db() as conn:
        mode = get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE
        return jsonify({"mode": mode})


@app.route("/settings/aspect_mode", methods=["GET"])
def get_aspect_mode_setting():
    """Return the current aspect mode ('vertical' | 'cinematic') for the worker to
    read per-job. Falls back to DEFAULT_ASPECT_MODE when never overridden."""
    with db_lock, get_db() as conn:
        mode = get_setting(conn, "aspect_mode") or DEFAULT_ASPECT_MODE
        return jsonify({"mode": mode})


@app.route("/settings/character_videos", methods=["GET"])
def get_character_videos_setting():
    """Whether characters may INITIATE sending videos in chat ('on' | 'off',
    toggled via /videos). Unset defaults to ON. Explicit user-requested
    /video <desc> jobs never consult this — they enqueue character_video
    directly and always run."""
    with db_lock, get_db() as conn:
        enabled = get_setting(conn, "character_videos") != "off"
        return jsonify({"enabled": enabled})


@app.route("/settings/workflow/<step_key>", methods=["GET"])
def get_workflow_setting(step_key):
    """Return the currently-selected strategy key for a generation step."""
    with db_lock, get_db() as conn:
        strat = get_workflow_strategy(conn, step_key)
        if strat is None:
            return jsonify({"error": f"unknown step: {step_key}"}), 404
        return jsonify({"step": step_key, "workflow": strat["key"], "label": strat["label"]})


@app.route("/settings/workflows", methods=["GET"])
def get_workflow_catalog():
    """Return the full workflow catalog (steps + strategies) for the worker
    and for debugging."""
    with db_lock, get_db() as conn:
        catalog = []
        for step in build_workflow_steps():
            cur = get_workflow_strategy(conn, step["key"])
            catalog.append({
                "key": step["key"],
                "label": step["label"],
                "default": step["default"],
                "selected": cur["key"],
                "strategies": step["strategies"],
            })
        return jsonify({"steps": catalog})


def get_in_progress_session(conn, chat_id):
    """Most recent non-active session for this chat, if a character creation
    flow is currently underway. Assumes one concurrent flow per chat (fine
    for the single-user v1 scope)."""
    return conn.execute(
        "SELECT * FROM sessions WHERE chat_id = ? AND state != 'active' "
        "ORDER BY updated_at DESC LIMIT 1",
        (chat_id,),
    ).fetchone()


def get_active_character(conn, chat_id):
    """Return the single active character shared by Telegram and web."""
    shared_id=get_setting(conn,"active_character_id")
    if shared_id and str(shared_id).isdigit():
        shared=conn.execute("SELECT * FROM characters WHERE id=?",(int(shared_id),)).fetchone()
        if shared:return shared
    legacy=conn.execute(
        "SELECT c.* FROM characters c JOIN sessions s ON s.character_id = c.id "
        "WHERE s.chat_id = ? AND s.state = 'active' ORDER BY s.updated_at DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if not legacy:
        legacy=conn.execute(
            "SELECT c.* FROM characters c JOIN sessions s ON s.character_id=c.id "
            "WHERE s.state='active' ORDER BY s.updated_at DESC,s.id DESC LIMIT 1"
        ).fetchone()
    if legacy:set_shared_active_character(conn,legacy["id"])
    return legacy


def set_shared_active_character(conn,character_id):
    """Update the global single-user selection while retaining context provenance."""
    if character_id is None:conn.execute("DELETE FROM settings WHERE key='active_character_id'")
    else:set_setting(conn,"active_character_id",str(int(character_id)))


def ensure_transport_context(conn,transport,external_id,display_name=None):
    conn.execute("INSERT OR IGNORE INTO contexts(transport,external_id,display_name) VALUES(?,?,?)",(transport,str(external_id),display_name))
    return conn.execute("SELECT id FROM contexts WHERE transport=? AND external_id=?",(transport,str(external_id))).fetchone()[0]


def touch_session(conn, session_id, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE sessions SET {sets}, updated_at = ? WHERE id = ?",
        (*fields.values(), datetime.utcnow().isoformat(), session_id),
    )


# ---------------------------------------------------------------------------
# Telegram long-poll listener (runs in a background thread)
# ---------------------------------------------------------------------------

def telegram_listener():
    offset = None
    print("[telegram] listener started", flush=True)
    #TRACK_LISTENER_STARTED
    while True:
        touch_heartbeat("vps")
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
            #TRACK_getUpdates_OK
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                callback=update.get("callback_query")
                if callback:
                    try:handle_callback_query(callback)
                    except Exception as e:
                        print(f"[telegram] callback error: {type(e).__name__}: {e}",flush=True)
                    offset=update["update_id"]+1
                    continue
                msg = update.get("message")
                if not msg or "text" not in msg:
                    # Non-text updates are safe to skip; confirm them immediately.
                    offset = update["update_id"] + 1
                    continue

                chat_id = msg["chat"]["id"]
                text = msg["text"].strip()
                try:
                    handle_message(chat_id, text)
                except Exception as e:  # noqa: BLE001
                    # One poison update (corrupt session JSON, unexpected shape)
                    # must NOT kill the listener thread — the bot would go
                    # silently dead until restart. Tell the user, keep polling.
                    print(f"[telegram] handle_message error: {type(e).__name__}: {e}")
                    try:
                        send_telegram_message(
                            chat_id,
                            "Something went wrong on my end — please try that again.",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                # Confirm the offset only AFTER handling so a crash between
                # fetch and handle re-delivers the message on next poll
                # instead of losing it forever.
                offset = update["update_id"] + 1

        except Exception as e:
            print(f"[telegram] poll error: {type(e).__name__} — retrying in 3s", flush=True)
            time.sleep(3)


from contextvars import ContextVar
_job_settings = ContextVar("job_settings", default=None)


def capture_job_settings(conn):
    return {
        "content_mode": get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE,
        "aspect_mode": get_setting(conn, "aspect_mode") or DEFAULT_ASPECT_MODE,
        "model": get_setting(conn, "current_model") or os.environ.get("OLLAMA_MODEL", "qwen3-14b-64k"),
        "character_videos": get_setting(conn, "character_videos") != "off",
        "workflows": {step["key"]: get_workflow_strategy(conn, step["key"])["key"]
                      for step in build_workflow_steps()},
    }


def enqueue_job(conn, chat_id, job_type, weight, payload, priority=5, context_id=None):
    if context_id is None and chat_id:
        context_id=ensure_transport_context(conn,"telegram",chat_id)
    payload = dict(payload)
    payload.setdefault("settings_snapshot", _job_settings.get() or capture_job_settings(conn))
    if job_type == "character_chat_intent" and "user_message_id" not in payload:
        if context_id is not None:
            message = conn.execute("SELECT id FROM messages WHERE context_id=? AND character_id=? AND role='user' ORDER BY id DESC LIMIT 1", (context_id, payload["character_id"])).fetchone()
        else:
            message = conn.execute("SELECT id FROM messages WHERE chat_id=? AND character_id=? AND role='user' ORDER BY id DESC LIMIT 1", (chat_id, payload["character_id"])).fetchone()
        if message:
            payload["user_message_id"] = message[0]
    cur = conn.execute(
        "INSERT INTO jobs (chat_id, context_id, job_type, weight, priority, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, context_id, job_type, weight, priority, json.dumps(payload)),
    )
    return cur.lastrowid


def record_message(conn, character_id, chat_id, role, content, kind=None, context_id=None):
    """Append one turn to a chat history (VPS is the single writer). `character_id`
    is required for in-character chats; pass None for the bare llm_chat fallback,
    which is keyed by chat_id alone."""
    if context_id is None and chat_id:
        context_id=ensure_transport_context(conn,"telegram",chat_id)
    cur = conn.execute(
        "INSERT INTO messages (character_id, chat_id, context_id, role, kind, content) VALUES (?, ?, ?, ?, ?, ?)",
        (character_id, chat_id, context_id, role, kind, content),
    )
    return cur.lastrowid


def build_chat_history(conn, character_id, chat_id, limit=32):
    """Read the most recent message exchanges and render them as a compact
    multi-turn transcript for the LLM (oldest -> newest), e.g.:
        user: hey
        assistant: hey babe
        ...(media entries bracketed)...
    In-character chats are shared across transports by character. The bare
    llm_chat fallback remains keyed by chat_id. Returns '' when empty."""
    if character_id is not None:
        rows = conn.execute(
            "SELECT role, kind, content FROM messages "
            "WHERE character_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (character_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, kind, content FROM messages "
            "WHERE character_id IS NULL AND chat_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    lines = []
    for r in reversed(rows):  # oldest -> newest
        tag = {"image": "[sent a photo]", "video": "[sent a video]"}.get(r["kind"], "")
        lines.append(f"{r['role']}: {tag}{r['content']}".strip())
    return "\n".join(lines)


def build_identity_instruction(concept: str, requested_change: str) -> str:
    # Identity comes from the reference, not the concept's old outfit/background.
    return build_image_prompt(requested_change)


GENDER_MARKERS = {
    "male": ["male", "man", "guy", "boy", "masculine", "he is", "his "],
    "female": ["female", "woman", "girl", "feminine", "lady", "she is", "her "],
}

# Strong, explicit gender nouns win outright; he/she pronoun counting below is
# only a fallback when no explicit noun appears (brittle substrings like
# "his "/"her " collide too easily in arbitrary concept prose).

def infer_gender_hint(concept: str) -> str:
    """Pick a voice-pool hint from the character concept text. Prefer an explicit
    gender noun (woman/female/girl vs man/male/guy) — that is authoritative — and
    only fall back to counting weak he/she pronouns when no explicit noun appears.
    This avoids the false-male/alt results that break-voice-sample bias caused for
    Sophie ('woman' -> alt, a 'his '/he collision) and Gleeta ('female alien' ->
    male, m=2 vs f=1)."""
    c = (concept or "").lower()
    # Explicit nouns first: the FIRST one mentioned decides (strongest signal).
    for tok in ("female", "woman", "girl", "lady", "male", "man", "guy", "boy"):
        if tok in c:
            return "female" if tok in ("female", "woman", "girl", "lady") else "male"
    # No explicit noun — fall back to weak pronoun/he-she counting.
    m = sum(1 for k in GENDER_MARKERS["male"] if k in c)
    f = sum(1 for k in GENDER_MARKERS["female"] if k in c)
    if m > f and m > 0:
        return "male"
    if f > m and f > 0:
        return "female"
    return "alt"  # ambiguous / non-binary / alien — mix both pools


from scripts.voice_reference import VOICE_REFERENCE_PASSAGE


def make_voice_sample_line(character_name: str, concept: str) -> str:
    """Longer, natural in-character line used as the TTS text for the voice
    samples. A few sentences (not a one-liner) so the character's timbre, pitch
    and cadence come through clearly when judging the candidate voices."""
    name = character_name or "my"
    return (
        f"Hi, I'm {name}. It's really nice to meet you. "
        f"{VOICE_REFERENCE_PASSAGE}"
    )


def build_voice_profile(concept: str, gender_hint: str, personality_notes: str = "") -> dict:
    """Extract OBJECTIVE acoustic attributes for the voice-sample engine from the
    character's concept + personality notes. Key insight (Qwen3 VoiceDesign): it
    responds to physical voice qualities (pitch / pace / age / characteristic),
    NOT subjective vibe-words ("sultry, breathy, dripping with desire") which the
    model renders as anime-style over-acting. So we map the character to explicit
    acoustic dimensions.

    IMPORTANT: the acoustic dimensions are derived from a deliberate
    personality-vibe → (pitch, pace, characteristic) map, NOT from substring
    collisions on appearance words. Words like "high", "bright", "delicate",
    "soft", "dark" describe the character's LOOK ("high-style hair", "delicate
    features") and must NOT flip the voice. Returns a dict consumed by the
    worker to build the 3 candidate descriptions."""
    concept = (concept or "").strip()
    notes = (personality_notes or "").strip()
    low = (concept + " " + notes).lower()
    gender_word = {"male": "male", "female": "female", "alt": "androgynous"}.get(gender_hint, "female")

    # ---- AGE (objective) ----
    age = "late 20s to early 30s"
    if any(w in low for w in ("young", "teen", "girlish", "petite", "cute", "early 20s",
                              "barely", "adolescent", "college", "schoolgirl")):
        age = "early 20s"
    elif any(w in low for w in ("mature", "older", "vintage", "1850", "1950", "pinup", "retro",
                                "lady", "middle-aged", "40s", "seductress")):
        age = "mid-30s to 40s"

    # ---- PRIMARY PERSONALITY VIBE → acoustic triple (pitch, pace, characteristic) ----
    # Pick ONE vibe class (first match wins, priority order). This is the reliable
    # mapping that yields the user's desired results (e.g. Sophie → low & languid).
    if any(w in low for w in ("alien", "extraterrestrial", "nonhuman", "robot", "android",
                              "cyborg", "creature", "eldritch", "monster")):
        vibe = "alien"
        pitch, pace, characteristic = "high", "fast", ("bright and clear with an unpredictable, "
                                                       "staccato, darting rhythm and a slightly "
                                                       "mechanical, odd cadence")
    elif any(w in low for w in ("seductive", "sultry", "nymph", "exhibition", "hypersexual",
                                "slutty", "breathless", "sex goddess", "sex worker", "desire",
                                "flirty and sexy", "loves attention", "show off", "hot")):
        # "Languid" types: unhurried, low, rich — matches the user's Sophie desire.
        vibe = "seductive"
        pitch, pace, characteristic = "medium-low", "slow", ("rich, warm and low with an unhurried, "
                                                             "mellowness")
    elif any(w in low for w in ("dominant", "professional", "confident", "command", "take charge",
                                "dominatrix", "assertive", "strong-willed", "in charge", "boss")):
        vibe = "confident"
        pitch, pace, characteristic = "medium", "medium", "clear, crisp and confident, steady and well-resonant"
    elif any(w in low for w in ("shy", "quiet", "soft-spoken", "gentle", "sweet", "reserved",
                                "innocent", "demure", "bashful", "timid")):
        vibe = "soft"
        pitch, pace, characteristic = "medium", "medium", "soft, gentle and light with a clear, delicate timbre"
    elif any(w in low for w in ("playful", "bubbly", "cheerful", "lively", "animated", "energetic",
                                "excitable", "peppy", "sprightly", "girl-next-door")):
        vibe = "playful"
        pitch, pace, characteristic = "high", "fast", "bright, lively and clear with a springy, cheerful energy"
    else:
        # No strong personality signal — a neutral, natural, low-default voice
        vibe = "neutral"
        pitch, pace, characteristic = "medium", "medium", "mellow and smooth"

    # ---- ANIME / CARTOON guard (relax realism only if concept is explicitly anime) ----
    anime = bool(any(w in low for w in ("anime", "cartoon", "chibi", "manga", "toy", "fictional cartoon")))

    return {
        "gender": gender_word,
        "age": age,
        "pitch": pitch,
        "pace": pace,
        "characteristic": characteristic,
        "vibe": vibe,
        "anime": anime,
    }


def make_voice_description(concept: str, gender_hint: str, character_name: str = "",
                           personality_notes: str = "") -> str:
    """Build the BASE natural-language voice description for the descriptive-voice
    engine (qwen3 VoiceDesign) from the character's objective acoustic profile.
    Uses physical voice qualities (pitch / pace / age / characteristic) — NOT
    subjective vibe-words that drive anime over-acting. The worker then builds
    3 distinct full descriptions from this."""
    prof = build_voice_profile(concept, gender_hint, personality_notes)
    g = prof["gender"]
    name_bit = f" for {character_name}" if character_name else ""
    if prof["vibe"] == "alien":
        # Higher, fast, darting, odd cadence — "natural but idiosyncratic"
        base = (f"A {g} speaking voice{name_bit}, {prof['age']}, {prof['pitch']} pitch "
                f"and a {prof['pace']} pace, {prof['characteristic']}, curious and "
                f"animated yet natural and grounded.")
    else:
        base = (f"A {g} speaking voice{name_bit}, {prof['age']}, {prof['pitch']} pitch "
                f"and a {prof['pace']} pace, {prof['characteristic']}, calm and natural "
                f"in conversation.")
    if prof["anime"]:
        realism = ("This character is an anime/cartoon style, so a light stylized tone "
                   "is acceptable, but keep it natural and charming rather than grating.")
    else:
        realism = ("This must sound like a REAL natural adult human voice, realistic and "
                   "believable — never anime-style, never cartoonish, never cutesy/chippy, "
                   "never overly theatrical or exaggerated. Keep it grounded, calm and natural.")
    return f"{base} {realism} Natural, expressive, speaking slowly and clearly."



from vps.web_api import create_web_blueprint, _speaker_preview
app.register_blueprint(create_web_blueprint(
    get_db, db_lock, get_setting,
    {"content_mode": DEFAULT_CONTENT_MODE, "aspect_mode": DEFAULT_ASPECT_MODE},
    enqueue_job, record_message, build_identity_instruction, set_setting,
    build_workflow_steps, get_workflow_strategy,
    get_active_character, set_shared_active_character,
))

def _character_scene(conn, character_id):
    if not character_id:
        return None
    return conn.execute("SELECT * FROM character_scenes WHERE character_id=?", (character_id,)).fetchone()


def _scene_media_request(conn, active_char, requested):
    """Apply the same reusable, character-free scene contract used by web."""
    aspect = get_setting(conn, "aspect_mode") or DEFAULT_ASPECT_MODE
    scene = active_scene_view(conn, aspect)
    if not scene:
        return requested, active_char["base_image_path"], None, None
    effective = (f"Maintain this reusable environment: {scene['description']}\n"
                 f"Latest requested action: {requested}")
    asset = scene.get("active_asset") or {}
    return effective, active_char["base_image_path"], scene, (
        asset.get("reference_image") if asset.get("ready") else None
    )

def _do_image(conn, chat_id: int, requested: str, active_char):
    """Generate an identity-locked image (or plain batch if no active char).
    Shared by the inline `/image <desc>` command and the menu-triggered
    awaiting_image_desc state."""
    if active_char and active_char["base_image_path"]:
        effective,reference,scene,scene_reference = _scene_media_request(conn,active_char,requested)
        instruction = build_image_prompt(effective, ((2, "the character"),), 1) if scene_reference else build_identity_instruction(active_char["concept"], effective)
        enqueue_job(
            conn, chat_id, "character_image_edit", "heavy",
            {"character_id":active_char["id"],"prompt":instruction,"reference_image":reference,
             "scene_context":scene["description"] if scene else "","scene_revision":scene["revision"] if scene else 0,
             "scene_id":scene["id"] if scene else None,"scene_aspect_mode":get_setting(conn,"aspect_mode") or DEFAULT_ASPECT_MODE,
             "scene_reference_image":scene_reference,"using_scene_anchor":bool(scene_reference)},
        )
    else:
        enqueue_job(conn, chat_id, "comfy_image_batch", "heavy", {"prompt": requested})
    _defer(conn, None, "msg", chat_id, "On it...")


def _do_video(conn, chat_id: int, requested: str, active_char):
    """Generate a speaking video. Shared by the inline `/video <desc>` command
    and the menu-triggered awaiting_video_desc state."""
    if not (active_char and active_char["base_image_path"]):
        _defer(conn, None, "msg", chat_id, "Need an active character with a reference photo first.")
        return
    # Two-stage: first generate an identity-locked scene image (same pipeline as
    # /image), then animate from that rather than animating off the static base.
    effective,reference,scene,scene_reference = _scene_media_request(conn,active_char,requested)
    scene_instruction = build_image_prompt(effective, ((2, "the character"),), 1) if scene_reference else build_identity_instruction(active_char["concept"], effective)
    enqueue_job(
        conn, chat_id, "character_video", "heavy",
        {
            "character_id":active_char["id"],
            "scene_prompt": scene_instruction,
            "motion_prompt": effective,
            "reference_image": reference,
            "concept": active_char["concept"],
            "voice_audio": active_char["voice_sample_path"] or None,
            "character_name": active_char["name"],
            "scene_context":scene["description"] if scene else "",
            "scene_revision":scene["revision"] if scene else 0,
            "scene_id":scene["id"] if scene else None,
            "scene_aspect_mode":get_setting(conn,"aspect_mode") or DEFAULT_ASPECT_MODE,
            "scene_reference_image":scene_reference,
            "using_scene_anchor":bool(scene_reference),
        },
        priority=10,  # lower priority than everything else — video is slow, shouldn't block quick replies
    )
    _defer(conn, None, "msg", chat_id, "On it... generating the scene, then animating it (a few minutes).")


def job_status_text(conn, chat_id):
    rows = conn.execute("SELECT id,job_type,status,payload,progress_json,result FROM jobs WHERE chat_id=? ORDER BY id DESC LIMIT 5", (chat_id,)).fetchall()
    if not rows:
        return "No jobs yet."
    lines = ["Recent jobs:"]
    labels = {"queued": "Queued", "dispatched": "Preparing", "interrupted": "Needs recovery", "failed": "Failed", "done": "Completed"}
    for row in rows:
        state = labels.get(row["status"], row["status"])
        progress = json.loads(row["progress_json"] or "{}")
        if row["status"] == "dispatched":
            state = {"rendering": "Rendering", "rendering_video": "Rendering video",
                     "rendering_image": "Rendering image", "rendering_voice": "Generating voice",
                     "extracting_frame":"Preparing continuation frame",
                     "submitting": "Submitting render"}.get(progress.get("phase"), state)
            if progress.get("percent") is not None:
                state += f" ({progress['percent']}%)"
        deliveries = [r[0] for r in conn.execute("SELECT status FROM deliveries WHERE job_id=?", (row["id"],))]
        if deliveries and row["status"] == "done":
            if "failed" in deliveries:
                state = "Delivery failed; result retained"
            elif "sending" in deliveries:
                state = "Uploading"
            elif "pending" in deliveries:
                state = "Waiting for delivery"
            elif all(d == "delivered" for d in deliveries):
                state = "Delivered"
        settings = json.loads(row["payload"]).get("settings_snapshot", {})
        mode = " / ".join(str(settings[k]) for k in ("content_mode", "aspect_mode") if k in settings)
        line=f"#{row['id']} {row['job_type']}: {state}" + (f" ({mode})" if mode else "")
        if row["status"]=="failed":
            result=json.loads(row["result"] or "{}")
            failure=str(result.get("text") or "No failure detail was recorded.")[:300]
            line += f"\n  {failure}"
            if not result.get("cancelled"):line += f"\n  Retry: /retry {row['id']}"
        elif row["status"] in ("queued","dispatched","interrupted"):
            line += f"\n  Cancel: /cancel {row['id']}"
        lines.append(line)
    return "\n".join(lines)


def _telegram_job(conn, chat_id, job_id=None, statuses=()):
    params=[chat_id];where="chat_id=?"
    if job_id is not None:where+=" AND id=?";params.append(job_id)
    if statuses:
        where+=" AND status IN (%s)" % ",".join("?"*len(statuses));params.extend(statuses)
    return conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT 1",params).fetchone()


def _cancel_telegram_job(conn, chat_id, job_id):
    row=_telegram_job(conn,chat_id,job_id,("queued","dispatched","interrupted"))
    if not row:return "That job is unavailable or has already finished."
    payload=json.loads(row["payload"] or "{}")
    conn.execute("UPDATE jobs SET status='failed',result=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                 (json.dumps({"text":"Cancelled by user","cancelled":True}),row["id"]))
    if payload.get("user_message_id"):
        conn.execute("DELETE FROM messages WHERE id=?",(payload["user_message_id"],))
    character_id=payload.get("character_id")
    creation=conn.execute("SELECT id FROM sessions WHERE chat_id=? AND character_id=? AND state!='active'",
                          (chat_id,character_id)).fetchone() if character_id else None
    if creation:
        conn.execute("DELETE FROM sessions WHERE id=?",(creation["id"],))
    if row["status"]=="dispatched" and row["weight"]=="heavy":
        comfy=get_setting(conn,"endpoint_comfyui") or DEFAULT_ENDPOINTS["comfyui"]
        try:requests.post(f"{comfy}/interrupt",timeout=5)
        except requests.RequestException:pass
    return f"Cancelled job #{row['id']}."


from vps.job_retry import restart_step


def _retry_telegram_job(conn,chat_id,job_id=None,scope="job"):
    row=_telegram_job(conn,chat_id,job_id,("failed",))
    if not row:return None,"No retryable failed job was found."
    result=json.loads(row["result"] or "{}")
    retryable={"profile_generate","edit_profile_generate","character_image_batch","promote_reference",
               "voice_sample_batch","promote_voice","promote_scene_anchor","character_chat_intent",
               "character_image_edit","character_video","multi_character_image","multi_character_video",
               "comfy_image_batch"}
    if result.get("cancelled") or row["job_type"] not in retryable:
        return None,"That job cannot be retried."
    payload=json.loads(row["payload"]);character_id=payload.get("character_id")
    if character_id and not conn.execute("SELECT 1 FROM characters WHERE id=?",(character_id,)).fetchone():
        return None,"The character used by that job no longer exists."
    creation_states={"profile_generate":"profile_generating","character_image_batch":"image_queued",
                     "promote_reference":"promoting_reference","voice_sample_batch":"voice_generating",
                     "promote_voice":"promoting_voice"}
    job_type=row["job_type"];state=creation_states.get(job_type)
    if scope=="step":
        if conn.execute("SELECT 1 FROM jobs WHERE context_id IS ? AND status IN ('queued','dispatched','interrupted')",(row["context_id"],)).fetchone() or conn.execute("SELECT 1 FROM sessions WHERE context_id IS ? AND state!='active'",(row["context_id"],)).fetchone():
            return None,"Finish the current generation or character setup first."
        try:job_type,state,payload=restart_step(conn,row)
        except ValueError as exc:return None,str(exc)
    if state:
        conn.execute("DELETE FROM sessions WHERE chat_id=? AND character_id=? AND state!='active'",(chat_id,character_id))
        conn.execute("INSERT INTO sessions(character_id,chat_id,context_id,state) VALUES(?,?,?,?)",
                     (character_id,chat_id,row["context_id"],state))
    payload["retry_of_job_id"]=row["id"]
    payload.pop("settings_snapshot",None)
    new_id=enqueue_job(conn,chat_id,job_type,row["weight"],payload,priority=row["priority"],
                       context_id=row["context_id"])
    return new_id,f"{'Selection step restarted' if scope=='step' else 'Retry queued'} as job #{new_id}."


def handle_callback_query(callback):
    """Start Telegram actions attached to delivered media."""
    callback_id=callback.get("id");data=str(callback.get("data") or "")
    message=callback.get("message") or {};chat_id=(message.get("chat") or {}).get("id")
    if callback_id:
        _telegram_send("answerCallbackQuery",json={"callback_query_id":callback_id},timeout=(5,10))
    if not chat_id or not data.startswith("continue:"):return
    value=data.partition(":")[2]
    if not value.isdigit():return
    artifact_id=int(value)
    with db_lock,get_db() as conn:
        source=conn.execute("""SELECT a.id FROM artifacts a JOIN jobs j ON j.id=a.job_id
          WHERE a.id=? AND a.kind='video' AND j.chat_id=?""",(artifact_id,chat_id)).fetchone()
        if not source:
            _defer(conn,None,"msg",chat_id,"That video is no longer available for continuation.")
            return
        active=_telegram_job(conn,chat_id,statuses=("queued","dispatched","interrupted"))
        if active:
            _defer(conn,None,"msg",chat_id,f"Finish or cancel job #{active['id']} before continuing a video.")
            return
        conn.execute("DELETE FROM sessions WHERE chat_id=? AND state='awaiting_video_continuation'",(chat_id,))
        conn.execute("INSERT INTO sessions(character_id,chat_id,state,pending_data) VALUES(NULL,?,'awaiting_video_continuation',?)",
                     (chat_id,json.dumps({"artifact_id":artifact_id})))
        _defer(conn,None,"msg",chat_id,
               "Describe only what happens in the next clip. Use actual character names and put every spoken line in quotes. Send /cancel to stop.")


def _enqueue_telegram_continuation(conn,chat_id,artifact_id,description,turns=None):
    source=conn.execute("""SELECT a.id,a.relative_path,a.character_id,a.job_id,j.job_type,j.payload
      FROM artifacts a JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND a.kind='video' AND j.chat_id=?""",
      (artifact_id,chat_id)).fetchone()
    if not source:return None,"That video is no longer available."
    original=json.loads(source["payload"]);character_id=source["character_id"] or original.get("character_id")
    message_id=record_message(conn,character_id,chat_id,"user",description,kind="video")
    payload=dict(original);payload.update({
        "character_id":character_id,"user_message_id":message_id,
        "continuation_artifact_id":artifact_id,"continuation_parent_job_id":source["job_id"],
        "continuation_relative_path":source["relative_path"],"continuation_request":description,
        "caption":"Continued video.",
    });payload.pop("retry_of_job_id",None);payload.pop("settings_snapshot",None)
    if source["job_type"]=="character_video":
        payload.update({"motion_prompt":description,"scene_prompt":description})
        payload.pop("spoken_line",None);payload.pop("video_line",None)
    elif source["job_type"]=="multi_character_video":
        turns=turns or []
        first_a=next((turn["line"] for turn in turns if turn["speaker"]=="a"),None)
        first_b=next((turn["line"] for turn in turns if turn["speaker"]=="b"),None)
        payload.update({"scene_text":description,"interaction_text":description,"dialogue_turns":turns,
                        "spoken_line_a":first_a,"spoken_line_b":first_b})
    else:return None,"That video type cannot be continued."
    job_id=enqueue_job(conn,chat_id,source["job_type"],"heavy",payload,priority=10)
    return job_id,f"Continuation queued as job #{job_id}."


def handle_message(chat_id: int, text: str):
    print(f";TRACK_handle_msg: chat={chat_id} text={text[:60]}", flush=True)
    with db_lock, get_db() as conn:
        command = text.strip().split(maxsplit=1)[0].split("@")[0].lower() if text.strip() else ""
        if command == "/status":
            _defer(conn, None, "msg", chat_id, job_status_text(conn, chat_id))
            return
        session = get_in_progress_session(conn, chat_id)

        # Commands must be handled before pending-state input. Otherwise a state
        # such as awaiting_multichar_scene consumes "/cancel" as the requested
        # scene and launches a bogus render. Accept Telegram's /cancel@Bot form.
        if command == "/cancel":
            parts=text.strip().split(maxsplit=1)
            if len(parts)==2:
                if not parts[1].isdigit():
                    _defer(conn,None,"msg",chat_id,"Use /cancel <job-id> to confirm cancellation.")
                else:_defer(conn,None,"msg",chat_id,_cancel_telegram_job(conn,chat_id,int(parts[1])))
                return
            if session is None:
                active=_telegram_job(conn,chat_id,statuses=("queued","dispatched","interrupted"))
                if active:_defer(conn,None,"msg",chat_id,f"Job #{active['id']} is active. Send /cancel {active['id']} to confirm cancellation.")
                else:_defer(conn, None, "msg", chat_id, "Nothing is waiting for input or generation.")
                return
            state = session["state"]
            cancellable = state.startswith("awaiting_") or state == "confirming_delete"
            if not cancellable:
                _defer(conn, None, "msg", chat_id, "That job has already started and cannot be cancelled here.")
                return
            draft_states = {
                "awaiting_name", "awaiting_concept", "awaiting_personality_notes",
                "awaiting_look_choice", "awaiting_voice_choice",
            }
            if session["character_id"] and state in draft_states:
                # A cancelled creation should not leave a half-built character
                # or its setup sessions visible in /characters.
                conn.execute("DELETE FROM sessions WHERE character_id = ?", (session["character_id"],))
                conn.execute("DELETE FROM characters WHERE id = ?", (session["character_id"],))
            else:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))
            _defer(conn, None, "msg", chat_id, "Cancelled.")
            return

        if command == "/retry":
            parts=text.strip().split()[1:];job_id=None;scope="job"
            if parts and parts[-1]=="step":scope="step";parts.pop()
            if len(parts)>1 or (parts and not parts[0].isdigit()):
                _defer(conn,None,"msg",chat_id,"Use /retry [job-id] [step]. Add step to restart look or voice selection.")
                return
            if parts:job_id=int(parts[0])
            _,message=_retry_telegram_job(conn,chat_id,job_id,scope)
            _defer(conn,None,"msg",chat_id,message)
            return

        # --- Mid-flow: route by state, regardless of what the text looks like ---
        if session is not None:
            state = session["state"]
            character_id = session["character_id"]

            if state == "awaiting_video_continuation":
                pending=json.loads(session["pending_data"] or "{}")
                artifact_id=pending.get("artifact_id")
                source=conn.execute("""SELECT j.job_type,j.payload FROM artifacts a JOIN jobs j ON j.id=a.job_id
                    WHERE a.id=? AND a.kind='video' AND j.chat_id=?""",(artifact_id,chat_id)).fetchone()
                if not source:
                    touch_session(conn,session["id"],state="active",pending_data=None)
                    _defer(conn,None,"msg",chat_id,"That source video is no longer available.")
                    return
                if source["job_type"]=="multi_character_video":
                    original=json.loads(source["payload"]);char_a=original.get("char_a") or {};char_b=original.get("char_b") or {}
                    normalized,turns=_speaker_preview(text,char_a,char_b,guess_ambiguous=False)
                    if turns:
                        pending.update({"description":normalized,"turns":turns,"char_a":char_a.get("name","First character"),
                                        "char_b":char_b.get("name","Second character")})
                        touch_session(conn,session["id"],state="awaiting_continuation_speakers",pending_data=json.dumps(pending))
                        listing="\n".join(f"{i}. “{turn['line']}”" for i,turn in enumerate(turns,1))
                        _defer(conn,None,"msg",chat_id,listing+f"\n\nFor each line, reply A for {pending['char_a']} or B for {pending['char_b']}, separated by commas. Example: A,B")
                        return
                job_id,message=_enqueue_telegram_continuation(conn,chat_id,artifact_id,text,turns=[])
                touch_session(conn,session["id"],state="active",pending_data=None)
                _defer(conn,None,"msg",chat_id,message)
                return

            if state == "awaiting_continuation_speakers":
                pending=json.loads(session["pending_data"] or "{}");turns=pending.get("turns") or []
                choices=[part.strip().lower() for part in text.split(",")]
                if len(choices)!=len(turns) or any(choice not in ("a","b") for choice in choices):
                    _defer(conn,None,"msg",chat_id,f"Reply with exactly {len(turns)} comma-separated choices using only A or B.")
                    return
                confirmed=[{"speaker":choice,"line":turn["line"]} for choice,turn in zip(choices,turns)]
                _,message=_enqueue_telegram_continuation(conn,chat_id,pending.get("artifact_id"),pending.get("description",""),confirmed)
                touch_session(conn,session["id"],state="active",pending_data=None)
                _defer(conn,None,"msg",chat_id,message)
                return

            if state == "awaiting_name":
                # From bare /newcharacter: save the name, then ask for look.
                if not text.strip():
                    _defer(conn, None, "msg", chat_id, "What's their name?")
                    return
                conn.execute("UPDATE characters SET name = ? WHERE id = ?", (text.strip(), character_id))
                touch_session(conn, session["id"], state="awaiting_concept")
                _defer(conn, None, "msg", chat_id, f"Got it — {text.strip()}. Now describe their look (appearance, style, vibe).")
                return

            if state == "awaiting_concept":
                conn.execute("UPDATE characters SET concept = ? WHERE id = ?", (text, character_id))
                touch_session(conn, session["id"], state="awaiting_personality_notes")
                _defer(conn, None, "msg", chat_id, "Got it. Now describe their personality in a few sentences.")
                return

            if state == "awaiting_personality_notes":
                conn.execute(
                    "UPDATE characters SET personality_notes = ? WHERE id = ?", (text, character_id)
                )
                touch_session(conn, session["id"], state="profile_generating")
                enqueue_job(
                    conn, chat_id, "profile_generate", "light",
                    {"character_id": character_id, "personality_notes": text},
                )
                _defer(conn, None, "msg", chat_id, "Generating personality profile...")
                return

            if state == "awaiting_model_choice":
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    chosen = pending[int(text) - 1]
                    set_setting(conn, "current_model", chosen)
                    touch_session(conn, session["id"], state="active", pending_data=None)  # terminal marker
                    _defer(conn, None, "msg", chat_id, f"Switched model to {chosen}.")
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(pending)}.")
                return

            if state == "awaiting_content_mode_choice":
                # /mode: text = current mode (from pending_data). 1=SFW, 2=NSFW.
                if text == "1":
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES ('content_mode', 'sfw') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    # Auto-reset image workflow if it's NSFW and now in SFW mode
                    cur_img = get_setting(conn, "workflow_image")
                    if cur_img and _infer_workflow_mode(cur_img) == "nsfw":
                        new_wf = _image_default_for_mode("sfw")
                        conn.execute(
                            "INSERT INTO settings (key, value) VALUES ('workflow_image', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (new_wf,),
                        )
                        conn.commit()
                        _defer(conn, None, "msg",
                            chat_id,
                            "Switched to **SFW** mode. Soft guardrails are active. "
                            f"Image workflow auto-reset to the SFW default.",
                        )
                    else:
                        conn.commit()
                        _defer(conn, None, "msg",
                            chat_id,
                            "Switched to **SFW** mode. Soft guardrails are active to keep the experience tasteful.",
                        )
                    touch_session(conn, session["id"], state="active", pending_data=None)
                elif text == "2":
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES ('content_mode', 'nsfw') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    # Auto-reset image workflow if it's SFW and now in NSFW mode
                    cur_img = get_setting(conn, "workflow_image")
                    if cur_img and _infer_workflow_mode(cur_img) == "sfw":
                        new_wf = _image_default_for_mode("nsfw")
                        conn.execute(
                            "INSERT INTO settings (key, value) VALUES ('workflow_image', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (new_wf,),
                        )
                        conn.commit()
                        _defer(conn, None, "msg",
                            chat_id,
                            "Switched to **NSFW** mode — full experience. "
                            f"Image workflow auto-reset to the NSFW default.",
                        )
                    else:
                        conn.commit()
                        _defer(conn, None, "msg", chat_id, "Switched to **NSFW** mode — full experience.")
                    touch_session(conn, session["id"], state="active", pending_data=None)
                else:
                    _defer(conn, None, "msg", chat_id, "Reply with 1 for SFW or 2 for NSFW.")
                return

            if state == "awaiting_aspect_mode_choice":
                # /aspect: text = current mode (from pending_data). 1=Vertical, 2=Cinematic.
                if text == "1":
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES ('aspect_mode', 'vertical') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    conn.commit()
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    _defer(conn, None, "msg",
                        chat_id,
                        "Switched to **Vertical** mode — portrait (3:4 / 9:16) images and video.",
                    )
                elif text == "2":
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES ('aspect_mode', 'cinematic') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    conn.commit()
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    _defer(conn, None, "msg",
                        chat_id,
                        "Switched to **Cinematic** mode — widescreen (16:9) images and video, "
                        "film-style prompt rendering.",
                    )
                else:
                    _defer(conn, None, "msg", chat_id, "Reply with 1 for Vertical or 2 for Cinematic.")
                return

            if state == "awaiting_endpoints_target":
                # Step 1 of /endpoints: which service? pending_data = ["ollama","comfyui"]
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    key = pending[int(text) - 1]
                    label = "Ollama" if key == "ollama" else "ComfyUI"
                    current = get_setting(conn, f"endpoint_{key}") or DEFAULT_ENDPOINTS[key]
                    conn.execute(
                        "UPDATE sessions SET pending_data = ? WHERE id = ?",
                        (json.dumps(key), session["id"]),
                    )
                    conn.execute(
                        "UPDATE sessions SET state = 'awaiting_endpoints_action' WHERE id = ?",
                        (session["id"],),
                    )
                    conn.commit()
                    _defer(conn, None, "msg",
                        chat_id,
                        f"{label} is currently:\n{current}\n\n"
                        "1. Keep this IP\n2. Enter new IP\n\nReply with a number.",
                    )
                else:
                    _defer(conn, None, "msg", chat_id, "Reply with 1 for Ollama or 2 for ComfyUI.")
                return

            if state == "awaiting_endpoints_action":
                # Step 2 of /endpoints: keep or change. pending_data = target key.
                key = json.loads(session["pending_data"] or '"ollama"')
                label = "Ollama" if key == "ollama" else "ComfyUI"
                current = get_setting(conn, f"endpoint_{key}") or DEFAULT_ENDPOINTS[key]
                if text == "1":
                    # Keep current: nothing to change.
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    _defer(conn, None, "msg", chat_id, f"{label} left at: {current}")
                elif text == "2":
                    conn.execute(
                        "UPDATE sessions SET state = 'awaiting_endpoints_newip' WHERE id = ?",
                        (session["id"],),
                    )
                    conn.commit()
                    _defer(conn, None, "msg",
                        chat_id,
                        f"Send the new {label} base URL (e.g. http://192.168.1.50:11434).",
                    )
                else:
                    _defer(conn, None, "msg", chat_id, "Reply with 1 to keep or 2 to enter a new IP.")
                return

            if state == "awaiting_endpoints_newip":
                # Step 3 of /endpoints: raw URL text. Save it and finish.
                key = json.loads(session["pending_data"] or '"ollama"')
                label = "Ollama" if key == "ollama" else "ComfyUI"
                new_url = text.strip()
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"endpoint_{key}", new_url),
                )
                conn.commit()
                touch_session(conn, session["id"], state="active", pending_data=None)
                _defer(conn, None, "msg", chat_id, f"{label} endpoint updated to:\n{new_url}")
                return

            if state == "awaiting_workflow_step":
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    step_key = pending[int(text) - 1]
                    step = workflow_step_by_key(step_key)
                    if step is None:
                        _defer(conn, None, "msg", chat_id, "That step isn't available.")
                    else:
                        # Video workflow is determined by current /aspect mode
                        # Show only the mode-appropriate reference-video option
                        # (no free choice now; kept extensible for future models).
                        strategies = list(step["strategies"])
                        if step_key == "video":
                            cur_aspect = get_setting(conn, "aspect_mode") or DEFAULT_ASPECT_MODE
                            match_key = {
                                "vertical": "presets/minimax_h3_ref2v_vertical.json",
                                "cinematic": "presets/minimax_h3_ref2v_widescreen.json",
                            }.get(cur_aspect)
                            filtered = [s for s in strategies if s["key"] == match_key]
                            if filtered:
                                strategies = filtered
                        listing = "\n".join(
                            f"{i}. {s['label']}" for i, s in enumerate(strategies, start=1)
                        )
                        cur = get_workflow_strategy(conn, step_key)
                        conn.execute(
                            "UPDATE sessions SET pending_data = ? WHERE id = ?",
                            (json.dumps(step_key), session["id"]),
                        )
                        conn.execute(
                            "UPDATE sessions SET state = 'awaiting_workflow_strategy' WHERE id = ?",
                            (session["id"],),
                        )
                        conn.commit()
                        _defer(conn, None, "msg",
                            chat_id,
                            f"Workflows for {step['label'].lower()}:\n\n"
                            f"{listing}\n\n"
                            f"Currently: {cur['label']}\nReply with a number to switch.",
                        )
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(pending)}.")
                return

            if state == "awaiting_workflow_strategy":
                # pending_data holds a JSON-encoded step key (e.g. "\"image\"").
                try:
                    step_key = json.loads(session["pending_data"] or "null")
                except (ValueError, TypeError):
                    step_key = session["pending_data"]
                step = workflow_step_by_key(step_key)
                # For video, constrain to the single mode-appropriate option
                # (mirrors the listing filter above): only that choice is valid.
                strategies = list(step["strategies"]) if step else []
                if step and step_key == "video":
                    cur_aspect = get_setting(conn, "aspect_mode") or DEFAULT_ASPECT_MODE
                    match_key = {
                        "vertical": "presets/minimax_h3_ref2v_vertical.json",
                        "cinematic": "presets/minimax_h3_ref2v_widescreen.json",
                    }.get(cur_aspect)
                    f2 = [s for s in strategies if s["key"] == match_key]
                    if f2:
                        strategies = f2
                if step is None:
                    _defer(conn, None, "msg", chat_id, "That step isn't available.")
                elif text.isdigit() and 1 <= int(text) <= len(strategies):
                    chosen = strategies[int(text) - 1]
                    # Mode-mismatch guard: if the user picks an image workflow
                    # that doesn't match the current content mode, warn and
                    # ask if they want to switch modes instead.
                    if step_key == "image":
                        cur_mode = get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE
                        wf_mode = _infer_workflow_mode(chosen["key"])
                        if wf_mode and wf_mode != cur_mode:
                            target_mode = "nsfw" if wf_mode == "nsfw" else "sfw"
                            _defer(conn, None, "msg",
                                chat_id,
                                f"⚠️ **{chosen['label']}** is a **{wf_mode.upper()}** workflow, "
                                f"but the system is currently in **{cur_mode.upper()}** mode.\n\n"
                                f"Use `/mode` to switch to **{target_mode.upper()}** mode first, "
                                f"or pick a **{cur_mode.upper()}** workflow instead."
                            )
                            touch_session(conn, session["id"], state="active", pending_data=None)
                            return
                    set_setting(conn, f"workflow_{step_key}", chosen["key"])
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    _defer(conn, None, "msg",
                        chat_id, f"Switched {step['label'].lower()} workflow to: {chosen['label']}."
                    )
                else:
                    _defer(conn, None, "msg",
                        chat_id, f"Reply with a number 1-{len(strategies)}."
                    )
                return


            if state in ("awaiting_multichar_a", "awaiting_multiimage_a"):
                # User picks the first character for a two-character render.
                image_flow = state == "awaiting_multiimage_a"
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    char_a = pending[int(text) - 1]
                    # Keep the eligible list shown by the command and remove the
                    # first choice. Re-querying through active Telegram sessions
                    # can collapse this to only the currently selected character,
                    # leaving no possible second choice.
                    char_list = [c for c in pending if c.get("id") != char_a.get("id")]
                    if not char_list:
                        touch_session(conn, session["id"], state="active", pending_data=None)
                        _defer(
                            conn, None, "msg", chat_id,
                            "No different eligible second character is available. "
                            "Create or finish another character, then start the command again.",
                        )
                        return
                    next_state = "awaiting_multiimage_b" if image_flow else "awaiting_multichar_b"
                    conn.execute(
                        "UPDATE sessions SET state = ?, pending_data = ? WHERE id = ?",
                        (next_state, json.dumps({"char_a": char_a, "char_list": char_list}), session["id"]),
                    )
                    conn.commit()
                    listing = "\n".join(
                        f"{i}. {c['name']}" for i, c in enumerate(char_list, start=1)
                    )
                    _defer(conn, None, "msg",
                        chat_id,
                        f"**First character:** {char_a['name']}.\n\n"
                        f"Now pick the **second** character:\n\n{listing}\n\nReply with a number.",
                    )
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(pending)}.")
                return

            if state in ("awaiting_multichar_b", "awaiting_multiimage_b"):
                # User picks the second character
                image_flow = state == "awaiting_multiimage_b"
                data = json.loads(session["pending_data"] or "{}")
                char_a = data.get("char_a", {})
                char_list = data.get("char_list", [])
                distinct_choices = [c for c in char_list if c.get("id") != char_a.get("id")]
                if not distinct_choices:
                    # Recover sessions created by older builds that stored only
                    # character A in the second-character menu.
                    requirements = "c.base_image_path IS NOT NULL"
                    if not image_flow:
                        requirements += " AND c.voice_sample_path IS NOT NULL"
                    rows = conn.execute(
                        f"SELECT c.id, c.name FROM characters c WHERE {requirements} "
                        "AND c.id != ? ORDER BY c.created_at DESC, c.id DESC",
                        (char_a.get("id"),),
                    ).fetchall()
                    distinct_choices = [{"id": r["id"], "name": r["name"]} for r in rows]
                    if not distinct_choices:
                        touch_session(conn, session["id"], state="active", pending_data=None)
                        _defer(
                            conn, None, "msg", chat_id,
                            "No different eligible second character is available. "
                            "Create or finish another character, then start the command again.",
                        )
                        return
                    data["char_list"] = distinct_choices
                    touch_session(conn, session["id"], pending_data=json.dumps(data))
                    listing = "\n".join(
                        f"{i}. {c['name']}" for i, c in enumerate(distinct_choices, start=1)
                    )
                    _defer(
                        conn, None, "msg", chat_id,
                        f"Pick the **second** character:\n\n{listing}\n\n"
                        "Reply with a number, or /cancel.",
                    )
                    return
                if text.isdigit() and 1 <= int(text) <= len(char_list):
                    char_b = char_list[int(text) - 1]
                    if char_b["id"] == char_a["id"]:
                        _defer(conn, None, "msg", chat_id, f"{char_b['name']} is already the first character. Pick a different one.")
                        return
                    # Now ask for the scene description
                    next_state = "awaiting_multiimage_scene" if image_flow else "awaiting_multichar_scene"
                    conn.execute(
                        "UPDATE sessions SET state = ?, pending_data = ? WHERE id = ?",
                        (next_state, json.dumps({"char_a": char_a, "char_b": char_b}), session["id"]),
                    )
                    conn.commit()
                    _defer(conn, None, "msg",
                        chat_id,
                        f"**Character A:** {char_a['name']}\n**Character B:** {char_b['name']}\n\n"
                        f"Describe the scene and interaction between them. "
                        + ("What are they doing, wearing, and where are they?\n\n" if image_flow else
                           "What are they doing? Where are they? What do they say to each other?\n\n")
                        + "Reply with your description, or type /cancel to abort.",
                    )
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(char_list)}.")
                return

            if state in ("awaiting_multichar_scene", "awaiting_multiimage_scene"):
                # User provides the scene description. Enqueue the selected
                # two-character media job.
                image_flow = state == "awaiting_multiimage_scene"
                data = json.loads(session["pending_data"] or "{}")
                char_a_id = data.get("char_a", {}).get("id")
                char_b_id = data.get("char_b", {}).get("id")
                char_a = conn.execute("SELECT * FROM characters WHERE id = ?", (char_a_id,)).fetchone()
                char_b = conn.execute("SELECT * FROM characters WHERE id = ?", (char_b_id,)).fetchone()
                if not char_a or not char_b:
                    _defer(conn, None, "msg", chat_id, "One of those characters no longer exists.")
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    return
                if not char_a["base_image_path"] or not char_b["base_image_path"]:
                    _defer(conn, None, "msg", chat_id, "Both characters need a reference photo first.")
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    return
                if (not image_flow
                        and (not char_a["voice_sample_path"] or not char_b["voice_sample_path"])):
                    _defer(conn, None, "msg", chat_id, "Both characters need a voice sample first.")
                    touch_session(conn, session["id"], state="active", pending_data=None)
                    return
                scene_text = text.strip()
                # The installed multi-character graph has exactly two identity
                # and voice-reference slots. Reject a third saved character here
                # while the user can still edit the scene, instead of allowing
                # the worker to misassign that character's dialogue.
                other_characters = conn.execute(
                    "SELECT DISTINCT c.id, c.name FROM characters c "
                    "JOIN sessions s ON s.character_id = c.id "
                    "WHERE s.chat_id = ? AND s.state = 'active' "
                    "AND c.id NOT IN (?, ?)",
                    (chat_id, char_a_id, char_b_id),
                ).fetchall()
                unselected = [
                    row["name"] for row in other_characters
                    if row["name"] and re.search(
                        rf"(?<!\w){re.escape(row['name'])}(?!\w)", scene_text, re.I
                    )
                ]
                if unselected:
                    names = ", ".join(unselected)
                    _defer(
                        conn, None, "msg", chat_id,
                        "This workflow supports exactly two characters. "
                        f"The scene mentions unselected character(s): {names}. "
                        f"Rewrite the scene using only {char_a['name']} and {char_b['name']}, "
                        "or /cancel and start again with a different pair.",
                    )
                    return
                # Extract interaction and spoken lines from freeform text (simple heuristic)
                interaction_text = scene_text
                # Enqueue the job
                job_payload = {
                    "character_id": char_a["id"],
                    "char_a": {
                            "name": char_a["name"],
                            "concept": char_a["concept"],
                            "base_image": char_a["base_image_path"],
                        },
                    "char_b": {
                            "name": char_b["name"],
                            "concept": char_b["concept"],
                            "base_image": char_b["base_image_path"],
                    },
                    "scene_text": scene_text,
                    "interaction_text": interaction_text,
                }
                if not image_flow:
                    job_payload["char_a"]["voice_sample"] = char_a["voice_sample_path"]
                    job_payload["char_b"]["voice_sample"] = char_b["voice_sample_path"]
                aspect=get_setting(conn,"aspect_mode") or DEFAULT_ASPECT_MODE
                reusable=active_scene_view(conn,aspect)
                asset=reusable.get("active_asset") if reusable else None
                job_payload.update({
                    "scene_id":reusable["id"] if reusable else None,
                    "scene_context":reusable["description"] if reusable else "",
                    "scene_revision":reusable["revision"] if reusable else 0,
                    "scene_aspect_mode":aspect,
                    "scene_reference_image":asset["reference_image"] if asset and asset.get("ready") else None,
                })
                enqueue_job(
                    conn, chat_id,
                    "multi_character_image" if image_flow else "multi_character_video",
                    "heavy", job_payload,
                )
                touch_session(conn, session["id"], state="active", pending_data=None)
                _defer(conn, None, "msg",
                    chat_id,
                    f"On it — generating an {'image' if image_flow else 'video'} of "
                    f"**{char_a['name']}** and **{char_b['name']}** "
                    f"in scene: {scene_text[:60]}{'...' if len(scene_text) > 60 else ''}",
                )
                return


            if state == "awaiting_character_switch":
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    chosen = pending[int(text) - 1]
                    target_session = conn.execute(
                        "SELECT * FROM sessions WHERE character_id = ? AND state = 'active' "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (chosen["id"],),
                    ).fetchone()
                    if target_session:
                        touch_session(conn, target_session["id"], state="active")
                    else:
                        telegram_context=ensure_transport_context(conn,"telegram",chat_id)
                        conn.execute("INSERT INTO sessions(character_id,chat_id,context_id,state) VALUES(?,?,?,'active')",(chosen["id"],chat_id,telegram_context))
                    set_shared_active_character(conn,chosen["id"])
                    telegram_context=ensure_transport_context(conn,"telegram",chat_id)
                    conn.execute("""INSERT INTO context_selections(context_id,character_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                      ON CONFLICT(context_id) DO UPDATE SET character_id=excluded.character_id,updated_at=CURRENT_TIMESTAMP""",(telegram_context,chosen["id"]))
                    touch_session(conn, session["id"], state="active", pending_data=None)  # resolve this picker
                    _defer(conn, None, "msg", chat_id, f"Switched to {chosen['name']}.")
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(pending)}.")
                return

            if state == "awaiting_delete_choice":
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    chosen = pending[int(text) - 1]
                    touch_session(conn, session["id"], state="confirming_delete", pending_data=json.dumps(chosen))
                    _defer(conn, None, "msg",
                        chat_id,
                        f"Delete {chosen['name']}? This can't be undone. Reply YES to confirm, anything else to cancel.",
                    )
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(pending)}.")
                return

            if state == "confirming_delete":
                chosen = json.loads(session["pending_data"] or "{}")
                if text.strip().upper() == "YES":
                    conn.execute("DELETE FROM sessions WHERE character_id = ?", (chosen["id"],))
                    conn.execute("DELETE FROM characters WHERE id = ?", (chosen["id"],))
                    _defer(conn, None, "msg", chat_id, f"Deleted {chosen['name']}.")
                else:
                    _defer(conn, None, "msg", chat_id, f"Cancelled. {chosen['name']} was not deleted.")
                touch_session(conn, session["id"], state="active", pending_data=None)  # resolve this picker
                return

            if state == "awaiting_edit_choice":
                pending = json.loads(session["pending_data"] or "[]")
                if text.isdigit() and 1 <= int(text) <= len(pending):
                    chosen = pending[int(text) - 1]
                    touch_session(conn, session["id"], state="awaiting_edit_field", pending_data=json.dumps(chosen))
                    _defer(conn, None, "msg",
                        chat_id, f"Edit {chosen['name']}'s: name, look, or personality? Reply with one of those words."
                    )
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with a number 1-{len(pending)}.")
                return

            if state == "awaiting_edit_field":
                chosen = json.loads(session["pending_data"] or "{}")
                field = text.strip().lower()
                if field not in ("name", "look", "personality"):
                    _defer(conn, None, "msg", chat_id, "Reply with one of: name, look, personality.")
                    return
                touch_session(conn, session["id"], state=f"awaiting_edit_{field}", pending_data=json.dumps(chosen))
                prompts = {
                    "name": "New name?",
                    "look": "New description of their look?",
                    "personality": "New personality description?",
                }
                _defer(conn, None, "msg", chat_id, prompts[field])
                return

            if state == "awaiting_edit_name":
                chosen = json.loads(session["pending_data"] or "{}")
                conn.execute("UPDATE characters SET name = ? WHERE id = ?", (text, chosen["id"]))
                touch_session(conn, session["id"], state="active", pending_data=None)
                _defer(conn, None, "msg", chat_id, f"Renamed {chosen['name']} to {text}.")
                return

            if state == "awaiting_edit_look":
                chosen = json.loads(session["pending_data"] or "{}")
                conn.execute("UPDATE characters SET concept = ? WHERE id = ?", (text, chosen["id"]))
                touch_session(conn, session["id"], state="active", pending_data=None)
                _defer(conn, None, "msg",
                    chat_id,
                    f"Updated {chosen['name']}'s look description. Note: their existing reference photo is "
                    f"unchanged — this affects future generations' text guidance, not the identity-lock image itself.",
                )
                return

            if state == "awaiting_edit_personality":
                chosen = json.loads(session["pending_data"] or "{}")
                conn.execute("UPDATE characters SET personality_notes = ? WHERE id = ?", (text, chosen["id"]))
                touch_session(conn, session["id"], state="active", pending_data=None)
                enqueue_job(
                    conn, chat_id, "edit_profile_generate", "light",
                    {"character_id": chosen["id"], "personality_notes": text, "name": chosen["name"]},
                )
                _defer(conn, None, "msg", chat_id, f"Regenerating {chosen['name']}'s profile...")
                return

            if state == "awaiting_choice":
                pending = json.loads(session["pending_data"] or "[]")
                if text.strip() == "0":
                    # REROLL look candidates: user doesn't like the choices.
                    # Re-run the batch with a fresh seed + new LLM variant
                    # descriptions (worker randomizes seed per submit). Reset to
                    # image_queued so the completion handler re-delivers + re-arms.
                    concept = conn.execute(
                        "SELECT concept, personality_notes FROM characters WHERE id = ?", (character_id,)
                    ).fetchone()
                    touch_session(conn, session["id"], state="image_queued", pending_data=None)
                    enqueue_job(
                        conn, chat_id, "character_image_batch", "heavy",
                        {"character_id": character_id, "prompt": concept["concept"], "personality_notes": concept["personality_notes"]},
                    )
                    _defer(conn, None, "msg", chat_id, "Rerolling look candidates...")
                elif text.isdigit() and 1 <= int(text) <= len(pending):
                    chosen = pending[int(text) - 1]
                    touch_session(conn, session["id"], state="promoting_reference")
                    enqueue_job(
                        conn, chat_id, "promote_reference", "heavy",
                        {"character_id": character_id, "filename": chosen},
                    )
                    _defer(conn, None, "msg", chat_id, "Locking in that reference image...")
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with 0 to reroll, or a number 1-{len(pending)}.")
                return

            if state == "awaiting_voice_choice":
                pending = json.loads(session["pending_data"] or "[]")
                if text.strip() == "0":
                    # REROOL voice candidates: user doesn't like the sample voices.
                    # Reconstruct the voice_batch payload from the DB (same fields
                    # the promote_reference flow uses at ~line 1768) and re-run so
                    # the TTS engine designs 3 NEW distinct voices.
                    char = conn.execute(
                        "SELECT concept, name, personality_notes FROM characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    concept, char_name = char["concept"], char["name"]
                    char_notes = char["personality_notes"] or ""
                    g_hint = infer_gender_hint(concept)
                    touch_session(conn, session["id"], state="voice_generating", pending_data=None)
                    enqueue_job(
                        conn, chat_id, "voice_sample_batch", "heavy",
                        {
                            "character_id": character_id,
                            "prompt": make_voice_sample_line(char_name, concept),
                            "gender_hint": g_hint,
                            "voice_profile": build_voice_profile(concept, g_hint, char_notes),
                            "voice_description": make_voice_description(
                                concept, g_hint, char_name, char_notes
                            ),
                        },
                    )
                    _defer(conn, None, "msg", chat_id, "Rerolling voice samples...")
                elif text.isdigit() and 1 <= int(text) <= len(pending):
                    chosen = pending[int(text) - 1]  # dict: {filename, subfolder}
                    # Trim the greeting from the chosen recording and store the
                    # remaining performance as the video voice reference.
                    touch_session(conn, session["id"], state="promoting_voice")
                    enqueue_job(
                        conn, chat_id, "promote_voice", "heavy",
                        {
                            "character_id": character_id,
                            "filename": chosen["filename"],
                            "subfolder": chosen.get("subfolder", ""),
                        },
                    )
                    _defer(conn, None, "msg", chat_id, "Locking in that voice sample...")
                else:
                    _defer(conn, None, "msg", chat_id, f"Reply with 0 to reroll, or a number 1-{len(pending)}.")
                return

            if state in ("awaiting_image_desc", "awaiting_video_desc"):
                # Menu-triggered bare /image or /video: the user's reply IS the
                # description. Route it through the same code path as the
                # inline-description commands, then resolve this prompting session.
                requested = text.strip()
                if not requested:
                    _defer(conn, None, "msg",
                        chat_id,
                        "What should the image show?" if state == "awaiting_image_desc"
                        else "What should the video show?",
                    )
                    return
                active_char = get_active_character(conn, chat_id)
                # Re-use the existing inline handlers by calling the shared logic.
                conn.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))  # resolve this prompting session
                conn.commit()
                if state == "awaiting_image_desc":
                    _do_image(conn, chat_id, requested, active_char)
                else:
                    _do_video(conn, chat_id, requested, active_char)
                return

            # profile_generating / image_queued / promoting_reference: a job is
            # already in flight, ignore chatter
            _defer(conn, None, "msg", chat_id, "Still working on the previous step, one sec...")
            return

        # --- No flow in progress: normal commands ---
        if text == "/debug":
            setting_key = f"debug_menu_{chat_id}"
            enabled = get_setting(conn, setting_key) != "on"
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (setting_key, "on" if enabled else "off"),
            )
            _defer(conn, None, "command_menu", chat_id,
                   "debug" if enabled else "everyday")
            _defer(conn, None, "msg", chat_id,
                   "Debug commands are now visible in the menu."
                   if enabled else "Debug commands are now hidden from the menu.")
            return

        if text == "/model":
            enqueue_job(conn, chat_id, "list_models", "light", {})
            _defer(conn, None, "msg", chat_id, "Checking available models...")
            return

        if text == "/workflow":
            # List the four generation steps; user picks one, then we list the
            # available workflows for that step (two-step numbered chooser).
            _steps = build_workflow_steps()
            listing = "\n".join(
                f"{i}. {s['label']}" for i, s in enumerate(_steps, start=1)
            )
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_workflow_step', ?)",
                (chat_id, json.dumps([s["key"] for s in _steps])),
            )
            conn.commit()
            _defer(conn, None, "msg",
                chat_id,
                "Choose which step to change the workflow for:\n\n"
                f"{listing}\n\nReply with a number.",
            )
            return

        if text == "/endpoints":
            # Two-step numbered chooser: pick Ollama/ComfyUI, then keep or change
            # its base URL. Mirrors the /model and /workflow state patterns.
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_endpoints_target', ?)",
                (chat_id, json.dumps(["ollama", "comfyui"])),
            )
            conn.commit()
            _defer(conn, None, "msg",
                chat_id,
                "Which endpoint do you want to change?\n\n"
                "1. Ollama\n2. ComfyUI\n\nReply with a number.",
            )
            return

        if text == "/mode":
            # Global SFW/NSFW content-mode switch. One-step numbered chooser.
            current_mode = get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_content_mode_choice', ?)",
                (chat_id, json.dumps(current_mode)),
            )
            conn.commit()
            _defer(conn, None, "msg",
                chat_id,
                f"The system is currently in {'NSFW' if current_mode == 'nsfw' else 'SFW'} mode.\n\n"
                "Switch to:\n"
                "1. SFW — Soft guardrails are active to keep the experience tasteful.\n"
                "2. NSFW — consenting-adult content\n\n"
                "Reply with a number.",
            )
            return

        if text == "/aspect":
            # Global Vertical/Cinematic aspect-mode switch (orthogonal to /mode).
            # One-step numbered chooser, mirroring /mode.
            current_mode = get_setting(conn, "aspect_mode") or DEFAULT_ASPECT_MODE
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_aspect_mode_choice', ?)",
                (chat_id, json.dumps(current_mode)),
            )
            conn.commit()
            _defer(conn, None, "msg",
                chat_id,
                f"The system is currently in **{'Cinematic' if current_mode == 'cinematic' else 'Vertical'}** mode.\n\n"
                "Switch to:\n"
                "1. Vertical — portrait (3:4 images / 9:16 video), standard prompt rendering.\n"
                "2. Cinematic — widescreen (16:9) images and video, film-style prompt rendering.\n\n"
                "Reply with a number.",
            )
            return

        if text == "/videos":
            # Character-initiated video toggle (global, like /mode). OFF stops
            # characters from CHOOSING video as their chat medium; explicit
            # /video <desc> requests are unaffected and always run.
            current = "off" if get_setting(conn, "character_videos") == "off" else "on"
            new = "off" if current == "on" else "on"
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('character_videos', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (new,),
            )
            conn.commit()
            if new == "off":
                _defer(conn, None, "msg",
                    chat_id,
                    "Character-initiated videos: **OFF** — characters will no longer choose to "
                    "send videos on their own. Explicit /video <description> requests still work.",
                )
            else:
                _defer(conn, None, "msg",
                    chat_id,
                    "Character-initiated videos: **ON** — characters may choose to send videos "
                    "in chat again (~15% of media replies).",
                )
            return

        if command == "/scene":
            argument = text.strip().split(maxsplit=1)[1].strip() if len(text.strip().split(maxsplit=1)) == 2 else ""
            action,_,value = argument.partition(" ")
            action=action.lower();value=value.strip()
            aspect=get_setting(conn,"aspect_mode") or DEFAULT_ASPECT_MODE
            scene=active_scene_view(conn,aspect)
            scenes=[scene_view(conn,row["id"],aspect) for row in conn.execute(
                "SELECT id FROM scenes ORDER BY updated_at DESC,id DESC")]
            if not action:
                if not scene:
                    status="No reusable scene is active."
                else:
                    asset=scene.get("active_asset") or {}
                    status=(f"Active scene: **{scene['name']}**\nDescription: {scene['description']}\n"
                            f"{aspect.title()} visual anchor: {'ready' if asset.get('ready') else 'none'}")
                _defer(conn,None,"msg",chat_id,status+"\n\nCommands:\n"
                    "/scene list\n/scene new <name> | <description>\n/scene select <number>\n"
                    "/scene set <description>\n/scene on\n/scene off\n/scene anchor\n"
                    "/scene use — approve the latest anchor candidate\n"
                    "/scene remove — remove this aspect's anchor\n/scene clear — delete active scene")
                return
            if action=="list":
                if not scenes:
                    _defer(conn,None,"msg",chat_id,"No reusable scenes yet. Use /scene new <name> | <description>.")
                else:
                    listing="\n".join(f"{i}. {item['name']}"+(" (active)" if scene and item["id"]==scene["id"] else "")
                                      for i,item in enumerate(scenes,1))
                    _defer(conn,None,"msg",chat_id,"Reusable scenes:\n"+listing+"\n\nUse /scene select <number>.")
                return
            if action in ("select","on"):
                if action=="on" and not value:
                    if scene:_defer(conn,None,"msg",chat_id,f"Scene **{scene['name']}** is already active.")
                    elif scenes:
                        set_setting(conn,"active_scene_id",str(scenes[0]["id"]))
                        _defer(conn,None,"msg",chat_id,f"Scene **{scenes[0]['name']}** is active.")
                    else:_defer(conn,None,"msg",chat_id,"No saved scenes. Use /scene new <name> | <description>.")
                    return
                if not value.isdigit() or not 1<=int(value)<=len(scenes):
                    _defer(conn,None,"msg",chat_id,f"Use /scene select followed by a number from 1 to {len(scenes)}.")
                    return
                chosen=scenes[int(value)-1];set_setting(conn,"active_scene_id",str(chosen["id"]))
                _defer(conn,None,"msg",chat_id,f"Scene **{chosen['name']}** is active for every character and multi-character job.")
                return
            if action=="new":
                name,separator,description=value.partition("|")
                if not separator or not name.strip() or not description.strip() or len(name.strip())>120 or len(description.strip())>2000:
                    _defer(conn,None,"msg",chat_id,"Use /scene new <name> | <description>.")
                    return
                cur=conn.execute("INSERT INTO scenes(name,description) VALUES(?,?)",(name.strip(),description.strip()))
                set_setting(conn,"active_scene_id",str(cur.lastrowid))
                _defer(conn,None,"msg",chat_id,f"Created and activated **{name.strip()}**. Use /scene anchor for a character-free {aspect} anchor.")
                return
            if action=="set":
                if not value or len(value)>2000:
                    _defer(conn,None,"msg",chat_id,"Use /scene set followed by a description of 2000 characters or fewer.")
                    return
                if scene:
                    if scene["description"]!=value:conn.execute("DELETE FROM scene_assets WHERE scene_id=?",(scene["id"],))
                    conn.execute("UPDATE scenes SET description=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(value,scene["id"]))
                    _defer(conn,None,"msg",chat_id,f"Updated **{scene['name']}**. Existing anchors were cleared because the environment changed.")
                else:
                    name=(value[:50].rstrip(" .") or "Telegram scene")
                    cur=conn.execute("INSERT INTO scenes(name,description) VALUES(?,?)",(name,value))
                    set_setting(conn,"active_scene_id",str(cur.lastrowid))
                    _defer(conn,None,"msg",chat_id,f"Created and activated **{name}**. Use /scene anchor for a character-free anchor.")
                return
            if action=="off":
                conn.execute("DELETE FROM settings WHERE key='active_scene_id'")
                _defer(conn,None,"msg",chat_id,"Reusable scene continuity is off. Saved scenes and anchors were kept.")
                return
            if action=="clear":
                if not scene:_defer(conn,None,"msg",chat_id,"No reusable scene is active.")
                else:
                    conn.execute("DELETE FROM settings WHERE key='active_scene_id'")
                    conn.execute("DELETE FROM scenes WHERE id=?",(scene["id"],))
                    _defer(conn,None,"msg",chat_id,f"Deleted scene **{scene['name']}** and its anchors.")
                return
            if action=="remove":
                if scene:conn.execute("DELETE FROM scene_assets WHERE scene_id=? AND aspect_mode=?",(scene["id"],aspect))
                _defer(conn,None,"msg",chat_id,f"The {aspect} anchor was removed. Text continuity and other aspect anchors remain.")
                return
            if action=="anchor":
                if not scene:
                    _defer(conn,None,"msg",chat_id,"Select or create a reusable scene first.")
                    return
                prompt=("Create a canonical, photorealistic establishing image of this environment: "+scene["description"]+". "
                    "The location must be completely unoccupied: no people, characters, faces, bodies, silhouettes, "
                    "reflections of people, or human figures. Preserve clear open staging areas where characters can be added later. "
                    f"Compose specifically for a {aspect} final frame.")
                enqueue_job(conn,chat_id,"comfy_image_batch","heavy",{
                    "prompt":prompt,"scene_anchor_candidate":True,"scene_id":scene["id"],
                    "scene_revision":scene["revision"],"scene_aspect_mode":aspect,
                    "caption":f"{scene['name']} - {aspect} anchor candidate. Use /scene use to approve it."})
                _defer(conn,None,"msg",chat_id,f"Generating a character-free {aspect} anchor candidate…")
                return
            if action=="use":
                if not scene:
                    _defer(conn,None,"msg",chat_id,"Select a reusable scene before approving an anchor.")
                    return
                artifact=conn.execute("""SELECT a.id FROM artifacts a JOIN jobs j ON j.id=a.job_id
                    WHERE a.kind='image' AND json_extract(j.payload,'$.scene_anchor_candidate')=1
                      AND json_extract(j.payload,'$.scene_id')=? AND json_extract(j.payload,'$.scene_aspect_mode')=?
                    ORDER BY a.id DESC LIMIT 1""",(scene["id"],aspect)).fetchone()
                if not artifact:
                    _defer(conn,None,"msg",chat_id,"No scene anchor candidate is available. Use /scene anchor first.")
                    return
                enqueue_job(conn,chat_id,"promote_scene_anchor","light",{
                    "scene_id":scene["id"],"aspect_mode":aspect,"artifact_id":artifact["id"],
                    "artifact_url":f"/api/v1/artifacts/{artifact['id']}"})
                _defer(conn,None,"msg",chat_id,"Approving the latest scene anchor…")
                return
            _defer(conn,None,"msg",chat_id,"Unknown scene action. Send /scene to see the available commands.")
            return

        if text == "/multivideo":
            # Two-character video: pick char A then char B.
            rows = conn.execute(
                "SELECT c.id, c.name FROM characters c WHERE c.base_image_path IS NOT NULL "
                "AND c.voice_sample_path IS NOT NULL ORDER BY c.created_at DESC, c.id DESC",
            ).fetchall()
            if len(rows)<2:
                _defer(conn, None, "msg", chat_id, "Create at least two characters with reference photos and voices first.")
                return
            char_list = [{"id": r["id"], "name": r["name"]} for r in rows]
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_multichar_a', ?)",
                (chat_id, json.dumps(char_list)),
            )
            conn.commit()
            listing = "\n".join(
                f"{i}. {c['name']}" for i, c in enumerate(char_list, start=1)
            )
            _defer(conn, None, "msg",
                chat_id,
                f"Pick the **first** character for a multi-character video:\n\n{listing}\n\nReply with a number.",
            )
            return

        if text == "/multiimage":
            rows = conn.execute(
                "SELECT c.id, c.name FROM characters c WHERE c.base_image_path IS NOT NULL "
                "ORDER BY c.created_at DESC, c.id DESC",
            ).fetchall()
            if len(rows) < 2:
                _defer(conn, None, "msg", chat_id, "Create at least two characters with reference photos first.")
                return
            char_list = [{"id": r["id"], "name": r["name"]} for r in rows]
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_multiimage_a', ?)",
                (chat_id, json.dumps(char_list)),
            )
            listing = "\n".join(
                f"{i}. {c['name']}" for i, c in enumerate(char_list, start=1)
            )
            _defer(conn, None, "msg", chat_id,
                   f"Pick the **first** character for a two-character image:\n\n"
                   f"{listing}\n\nReply with a number, or /cancel.")
            return

        if text == "/newcharacter" or text.startswith("/newcharacter "):
            # Bare /newcharacter -> collect the name as a second step first
            # (two-step flow: name, then physical description, then personality).
            # /newcharacter <name> -> skip straight to the physical description
            # (backward-compatible shortcut).
            name = text[len("/newcharacter "):].strip() if text.startswith("/newcharacter ") else ""
            cur = conn.execute("INSERT INTO characters (name) VALUES (?)", (name,))
            character_id = cur.lastrowid
            if name:
                conn.execute(
                    "INSERT INTO sessions (character_id, chat_id, state) VALUES (?, ?, 'awaiting_concept')",
                    (character_id, chat_id),
                )
                _defer(conn, None, "msg", chat_id, f"Creating {name}. Describe their look (appearance, style, vibe).")
            else:
                conn.execute(
                    "INSERT INTO sessions (character_id, chat_id, state) VALUES (?, ?, 'awaiting_name')",
                    (character_id, chat_id),
                )
                _defer(conn, None, "msg", chat_id, "Got it — what's their name?")
            return

        if text == "/characters":
            rows = conn.execute(
                "SELECT c.id, c.name, c.base_image_path FROM characters c "
                "WHERE c.base_image_path IS NOT NULL AND c.voice_sample_path IS NOT NULL "
                "ORDER BY c.created_at DESC, c.id DESC",
            ).fetchall()
            if not rows:
                _defer(conn, None, "msg", chat_id, "No active characters yet. Use /newcharacter <name> to create one.")
                return
            active_character=get_active_character(conn,chat_id)
            current_id = active_character["id"] if active_character else rows[0]["id"]
            char_list = [{"id": r["id"], "name": r["name"]} for r in rows]
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_character_switch', ?)",
                (chat_id, json.dumps(char_list)),
            )
            comfy_url = get_setting(conn, "endpoint_comfyui") or DEFAULT_ENDPOINTS["comfyui"]
            _defer(conn, None, "character_grid", chat_id, json.dumps({
                "chars": [{"name": r["name"], "base_image_path": r["base_image_path"]} for r in rows],
                "comfy_url": comfy_url}))
            listing = "\n".join(
                f"{i}. {c['name']}" + (" (current)" if c["id"] == current_id else "")
                for i, c in enumerate(char_list, start=1)
            )
            _defer(conn, None, "msg", chat_id, f"{listing}\n\nReply with a number to switch. After choosing a character, use /delete to remove one by name.")
            return

        if text == "/edit":
            rows = conn.execute(
                "SELECT c.id, c.name FROM characters c JOIN sessions s ON s.character_id = c.id "
                "WHERE s.chat_id = ? AND s.state = 'active' ORDER BY s.updated_at DESC",
                (chat_id,),
            ).fetchall()
            if not rows:
                _defer(conn, None, "msg", chat_id, "No characters to edit.")
                return
            char_list = [{"id": r["id"], "name": r["name"]} for r in rows]
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_edit_choice', ?)",
                (chat_id, json.dumps(char_list)),
            )
            listing = "\n".join(f"{i}. {c['name']}" for i, c in enumerate(char_list, start=1))
            _defer(conn, None, "msg", chat_id, f"Edit which character?\n{listing}\n\nReply with a number.")
            return

        if text.startswith("/delete "):
            name = text[len("/delete "):].strip()
            row = conn.execute(
                "SELECT c.id, c.name FROM characters c JOIN sessions s ON s.character_id = c.id "
                "WHERE s.chat_id = ? AND s.state = 'active' AND c.name = ? "
                "ORDER BY s.updated_at DESC LIMIT 1",
                (chat_id, name),
            ).fetchone()
            if row is None:
                _defer(conn, None, "msg", chat_id, f"No active character named '{name}'. Check /characters for the list.")
            else:
                conn.execute(
                    "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                    "VALUES (NULL, ?, 'confirming_delete', ?)",
                    (chat_id, json.dumps({"id": row["id"], "name": row["name"]})),
                )
                _defer(conn, None, "msg",
                    chat_id,
                    f"Delete {row['name']}? This can't be undone. Reply YES to confirm, anything else to cancel.",
                )
            return

        if text == "/delete":
            rows = conn.execute(
                "SELECT c.id, c.name FROM characters c JOIN sessions s ON s.character_id = c.id "
                "WHERE s.chat_id = ? AND s.state = 'active' ORDER BY s.updated_at DESC",
                (chat_id,),
            ).fetchall()
            if not rows:
                _defer(conn, None, "msg", chat_id, "No characters to delete.")
                return
            char_list = [{"id": r["id"], "name": r["name"]} for r in rows]
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_delete_choice', ?)",
                (chat_id, json.dumps(char_list)),
            )
            listing = "\n".join(f"{i}. {c['name']}" for i, c in enumerate(char_list, start=1))
            _defer(conn, None, "msg", chat_id, f"Delete which character?\n{listing}\n\nReply with a number.")
            return

        if text.startswith("/image "):
            # Inline description: run the image immediately (unchanged "as is" path).
            requested = text[len("/image "):].strip()
            active_char = get_active_character(conn, chat_id)
            _do_image(conn, chat_id, requested, active_char)
            return

        if text == "/image":
            # Menu-triggered bare /image: ask for a description first, then run it.
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state) VALUES (NULL, ?, 'awaiting_image_desc')",
                (chat_id,),
            )
            conn.commit()
            _defer(conn, None, "msg",
                chat_id,
                "What should the image show? Describe the scene (and I'll keep your active "
                "character's identity if one is set).",
            )
            return

        if text.startswith("/video "):
            # Inline description: run the video immediately (unchanged "as is" path).
            requested = text[len("/video "):].strip()
            active_char = get_active_character(conn, chat_id)
            _do_video(conn, chat_id, requested, active_char)
            return

        if text == "/video":
            # Menu-triggered bare /video: ask for a description first, then run it.
            conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state) VALUES (NULL, ?, 'awaiting_video_desc')",
                (chat_id,),
            )
            conn.commit()
            _defer(conn, None, "msg",
                chat_id,
                "What should the video show? Describe the scene (and I'll keep your active "
                "character's identity if one is set).",
            )
            return

        active_char = get_active_character(conn, chat_id)
        if active_char and not text.startswith("/"):
            # Natural language with an active character: a light LLM call decides
            # whether this wants a photo or is just conversation, and drafts the
            # in-character reply either way. Image generation (if needed) is a
            # separate heavy job, so the peak-hour gate still applies to the
            # actual GPU work — only the classification+chat step runs immediately.
            history = build_chat_history(conn, active_char["id"], chat_id)
            record_message(conn, active_char["id"], chat_id, "user", text, kind="chat")
            scene=_character_scene(conn,active_char["id"])
            enqueue_job(
                conn, chat_id, "character_chat_intent", "light",
                {
                    "character_id": active_char["id"],
                    "system_prompt": active_char["system_prompt"],
                    "concept": active_char["concept"],
                    "history": history,
                    "message": text,
                    "scene_context":scene["description"] if scene and scene["enabled"] else "",
                    "scene_revision":scene["revision"] if scene else 0,
                    "scene_reference_image":scene["anchor_reference_image"] if scene and scene["enabled"] else None,
                },
            )
            return

        # Bare fallback (no active character): a plain, stateless-style LLM reply.
        # Give it durable per-chat history too (character_id=None -> keyed by chat_id).
        history = build_chat_history(conn, None, chat_id)
        record_message(conn, None, chat_id, "user", text, kind="chat")
        enqueue_job(conn, chat_id, "llm_chat", "light", {"prompt": text, "history": history})
        _defer(conn, None, "msg", chat_id, "On it...")


# Populate the Telegram / command menu + autocomplete. Called once at startup.
EVERYDAY_COMMANDS = [
    {"command": "characters", "description": "List or select your characters"},
    {"command": "newcharacter", "description": "Create a new character"},
    {"command": "image", "description": "Generate an identity-locked image"},
    {"command": "multiimage", "description": "Generate a two-character image"},
    {"command": "video", "description": "Generate a speaking video"},
    {"command": "scene", "description": "Configure scene continuity"},
    {"command": "multivideo", "description": "Generate a two-character video"},
    {"command": "edit", "description": "Edit the current character"},
    {"command": "mode", "description": "Switch SFW / NSFW mode"},
    {"command": "aspect", "description": "Switch Vertical / Cinematic mode"},
    {"command": "videos", "description": "Toggle character-initiated videos"},
    {"command": "delete", "description": "Delete a character"},
    {"command": "debug", "description": "Show or hide advanced commands"},
]

DEBUG_COMMANDS = [
    {"command": "status", "description": "Show jobs and delivery status"},
    {"command": "retry", "description": "Retry the latest failed job"},
    {"command": "model", "description": "Switch the LLM model"},
    {"command": "workflow", "description": "Switch generation workflows"},
    {"command": "endpoints", "description": "Change Ollama/ComfyUI endpoints"},
]


def _post_command_menu(commands, scope=None):
    payload = {"commands": commands}
    if scope is not None:
        payload["scope"] = scope
    resp = requests.post(f"{TELEGRAM_API}/setMyCommands", json=payload, timeout=15)
    data = resp.json()
    print("[telegram] setMyCommands ->", resp.status_code, data.get("ok"))
    return bool(resp.ok and data.get("ok"))


def send_telegram_command_menu(chat_id, menu):
    """Apply a per-chat menu; called by DeliveryWorker outside db_lock."""
    commands = EVERYDAY_COMMANDS + DEBUG_COMMANDS if menu == "debug" else EVERYDAY_COMMANDS
    return _post_command_menu(commands, {"type": "chat", "chat_id": chat_id})


def setup_telegram_commands():
    """Register the bot's /commands so the 🎛️ menu button and typing '/' both
    show the real command list (setMyCommands). Failures are non-fatal: the
    bot still works, just without the fancy menu."""
    try:
        return _post_command_menu(EVERYDAY_COMMANDS)
    except Exception as e:
        print(f"[telegram] setMyCommands failed: {type(e).__name__}")
        return False


def _comfy_view_bytes(comfy_url: str, filename: str, ftype: str = "input") -> bytes:
    """Fetch a file served by ComfyUI (/view). base_image_path lives in the
    input dir; outputs (e.g. promoted clips) use type=output. Raises on HTTP
    error so callers can degrade gracefully."""
    resp = requests.get(
        f"{comfy_url}/view",
        params={"filename": filename, "type": ftype},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def build_character_grid(chars: list, comfy_url: str, cell: int = 240,
                         cols: int = 3, pad: int = 12, label_h: int = 40) -> bytes:
    """Compose a numbered contact-sheet of character base images into PNG bytes
    for Telegram. `chars` is a list of dicts with keys: name, base_image_path,
    and optionally caption. Each cell crop-covers the base image square with a
    number + name label underneath. Returns PNG bytes, or None if chars is empty.
    Characters without a usable image get a labelled placeholder tile."""
    def _load_font(sz: int):
        for f in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                  "C:/Windows/Fonts/segoeui.ttf"):
            if os.path.exists(f):
                try:
                    return ImageFont.truetype(f, sz)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _cover(src: bytes, size: int):
        im = Image.open(io.BytesIO(src)).convert("RGB")
        w, h = im.size
        scale = size / min(w, h)
        nw, nh = int(w * scale), int(h * scale)
        im = im.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - size) // 2, (nh - size) // 2
        return im.crop((left, top, left + size, top + size))

    if not chars:
        return None
    n = len(chars)
    rows = math.ceil(n / cols)
    W = cols * (cell + pad) + pad
    H = rows * (cell + label_h + pad) + pad
    sheet = Image.new("RGB", (W, H), (26, 26, 32))
    draw = ImageDraw.Draw(sheet)
    fontb = _load_font(18)
    fonts = _load_font(14)
    for i, c in enumerate(chars):
        col, rw = i % cols, i // cols
        x = pad + col * (cell + pad)
        y = pad + rw * (cell + label_h + pad)
        img = None
        bpath = c.get("base_image_path")
        if bpath:
            try:
                img = _cover(_comfy_view_bytes(comfy_url, bpath, "input"), cell)
            except Exception:
                img = None
        if img is None:
            img = Image.new("RGB", (cell, cell), (70, 70, 80))
            ImageDraw.Draw(img).text((10, cell // 2), "no img", fill=(200, 200, 210), font=fonts)
        sheet.paste(img, (x, y))
        name = c.get("name") or "?"
        draw.text((x + 8, y + cell + 4), f"{i + 1}. {name}", fill=(235, 235, 242), font=fontb)
        if c.get("caption"):
            draw.text((x + 8, y + cell + 4 + 20), (c["caption"] or "")[:42], fill=(140, 140, 155), font=fonts)
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


def send_character_grid(chat_id, value):
    body = json.loads(value)
    image = build_character_grid(body["chars"], body["comfy_url"])
    return send_telegram_photo(chat_id, image, caption="Your characters — reply with a number to switch.") if image else True


def _telegram_send(method, **kwargs):
    if not TELEGRAM_ENABLED:
        return False
    try:
        response = requests.post(f"{TELEGRAM_API}/{method}", **kwargs)
        response.raise_for_status()
        return bool(response.json().get("ok"))
    except (requests.exceptions.RequestException, ValueError) as exc:
        print(f"[telegram] {method} failed: {type(exc).__name__}", flush=True)
        return False


def send_telegram_message(chat_id: int, text: str):
    return _telegram_send("sendMessage", json={"chat_id": chat_id, "text": text}, timeout=(10, 20))


def _send_media(kind, chat_id, content, filename, mime, caption, timeout, reply_markup=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return _telegram_send("send" + kind.capitalize(), data=data,
                          files={kind: (filename, content, mime)}, timeout=(10, timeout))


def send_telegram_photo(chat_id: int, image_bytes: bytes, caption: str = None):
    return _send_media("photo", chat_id, image_bytes, "result.png", "image/png", caption, 45)


def send_telegram_video(chat_id: int, video_bytes: bytes, caption: str = None, reply_markup=None):
    return _send_media("video", chat_id, video_bytes, "result.mp4", "video/mp4", caption, 180,
                       reply_markup=reply_markup)


def send_telegram_audio(chat_id: int, audio_bytes: bytes, caption: str = None):
    flac = audio_bytes.startswith(b"fLaC")
    return _send_media("audio", chat_id, audio_bytes, "voice.flac" if flac else "voice.wav",
                       "audio/flac" if flac else "audio/wav", caption, 60)


# ---------------------------------------------------------------------------
# Worker-facing API (called by the GPU box over Tailscale)
# ---------------------------------------------------------------------------

@app.route("/debug/peak-status", methods=["GET"])
def peak_status():
    return jsonify({"is_peak_hours": is_peak_hours(), "fake_now": os.environ.get("FAKE_NOW")})


STALE_DISPATCH_SECONDS = 600
LEASE_SECONDS = 180


def requeue_stale_jobs(conn):
    # Expired media work is held for inspection: ComfyUI may still be rendering.
    # Never enqueue a second render solely because its worker stopped reporting.
    cutoff = (datetime.utcnow() - timedelta(seconds=STALE_DISPATCH_SECONDS)).isoformat()
    rows = conn.execute("""SELECT id,weight FROM jobs WHERE status='dispatched' AND
        ((lease_expires_at IS NOT NULL AND lease_expires_at < ?) OR
         (lease_expires_at IS NULL AND datetime(dispatched_at) < datetime(?)))""",
         (time.time(), cutoff)).fetchall()
    for row in rows:
        if row["weight"] == "heavy":
            conn.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE jobs SET status='queued',dispatched_at=NULL,lease_token=NULL,lease_expires_at=NULL,worker_id=NULL WHERE id=?", (row["id"],))


@app.route("/jobs/<int:job_id>/lease", methods=["POST"])
def renew_lease(job_id):
    body = request.get_json(force=True)
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT status,lease_token FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] != "dispatched" or not body.get("lease_token") or row["lease_token"] != body["lease_token"]:
            return jsonify({"error": "lease no longer owned"}), 409
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (time.time() + LEASE_SECONDS, job_id))
        if "progress" in body:
            conn.execute("UPDATE jobs SET progress_json=? WHERE id=?", (json.dumps(body["progress"]), job_id))
        return jsonify({"status": "ok"})


@app.route("/jobs/recovery", methods=["GET"])
def recovery_jobs():
    with db_lock, get_db() as conn:
        requeue_stale_jobs(conn)
        rows = conn.execute("SELECT id,job_type,status,progress_json,lease_token FROM jobs WHERE status='interrupted' ORDER BY id").fetchall()
        return jsonify([dict(row) for row in rows])


@app.route("/jobs/<int:job_id>/recover", methods=["POST"])
def recover_interrupted_job(job_id):
    body=request.get_json(force=True)
    with db_lock,get_db() as conn:
        row=conn.execute("SELECT status,lease_token FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not row:return jsonify({"error":"unknown job"}),404
        if row["status"]!="interrupted" or not body.get("lease_token") or row["lease_token"]!=body["lease_token"]:
            return jsonify({"error":"job is not owned interrupted work"}),409
        conn.execute("""UPDATE jobs SET status='queued',dispatched_at=NULL,lease_token=NULL,
          lease_expires_at=NULL,worker_id=NULL,progress_json=NULL WHERE id=?""",(job_id,))
        return jsonify({"status":"queued"})

@app.route("/jobs/next", methods=["GET"])
def next_job():
    """Worker polls this. Claims the highest-priority (lowest number), oldest
    queued job atomically. During peak hours (16:00-21:00 America/Los_Angeles),
    only 'light' jobs are released — 'heavy' jobs stay queued until the window
    closes."""
    with db_lock, get_db() as conn:
        requeue_stale_jobs(conn)

        if is_peak_hours():
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' AND weight = 'light' "
                "AND COALESCE(CAST(json_extract(payload,'$.internal_retry_after') AS REAL),0) <= ? "
                "ORDER BY priority ASC, created_at ASC LIMIT 1", (time.time(),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' "
                "AND COALESCE(CAST(json_extract(payload,'$.internal_retry_after') AS REAL),0) <= ? "
                "ORDER BY priority ASC, created_at ASC LIMIT 1", (time.time(),)
            ).fetchone()

        if row is None:
            return jsonify(None)

        payload = json.loads(row["payload"])
        if row["job_type"] == "character_chat_intent":
            # Completed images only, scoped to the same character and conversation.
            images = conn.execute(
                "SELECT payload FROM jobs WHERE job_type='character_image_edit' "
                "AND status='done' AND id < ? AND context_id IS ? "
                "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.job_id=jobs.id AND d.status != 'delivered') "
                "AND json_extract(payload,'$.character_id')=? "
                "ORDER BY id DESC LIMIT 3",
                (row["id"], row["context_id"], payload["character_id"]),
            ).fetchall()
            payload["recent_visual_states"] = [
                json.loads(image["payload"])["visual_state"] for image in reversed(images)
                if isinstance(json.loads(image["payload"]).get("visual_state"), dict)
            ]
            payload["recent_image_descriptions"] = [
                (json.loads(image["payload"]).get("visual_state") or {}).get("description")
                or json.loads(image["payload"]).get("prompt", "")
                for image in reversed(images)
            ]
        if row["job_type"] == "character_chat_intent" and payload.get("user_message_id"):
            turns = conn.execute("SELECT role,content FROM messages WHERE character_id=? AND (id < ? OR role='assistant') ORDER BY id DESC LIMIT 32",
                (payload["character_id"], payload["user_message_id"])).fetchall()
            payload["conversation_turns"] = [dict(turn) for turn in reversed(turns)]
        token = uuid.uuid4().hex
        conn.execute(
            "UPDATE jobs SET status='dispatched',dispatched_at=?,lease_token=?,lease_expires_at=?,worker_id=? WHERE id=?",
            (datetime.utcnow().isoformat(), token, time.time() + LEASE_SECONDS,
             request.args.get("worker_id", "legacy"), row["id"]),
        )
        return jsonify(
            {
                "id": row["id"],
                "lease_token": token,
                "chat_id": row["chat_id"],
                "context_id": row["context_id"],
                "job_type": row["job_type"],
                "payload": payload,
            }
        )


class InvalidMediaPlan(ValueError):
    """An authored follow-up plan cannot safely be rendered."""


def validate_followup_instruction(concept, scene, latest_request="", reply="", mode=None):
    try:
        scene = compose_chat_media_scene(scene, latest_request, reply, mode)
        return build_identity_instruction(concept, scene)
    except ValueError as exc:
        raise InvalidMediaPlan(str(exc)) from exc


@app.route("/jobs/<int:job_id>/complete", methods=["POST"])
def complete_job(job_id):
    # The result, session changes, and pending deliveries commit together.
    # No network send or background shared-list flush occurs in this request.
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        settings = json.loads(row[0]).get("settings_snapshot") if row else None
    token = _job_settings.set(settings)
    try:
        try:
            return _complete_job_locked(job_id)
        except InvalidMediaPlan as exc:
            # The first transaction rolled back. Resolve the invalid plan through
            # the durable reply path so completion retries can drain.
            body = dict(request.get_json(force=True))
            # Keep the successful character reply. A fresh planning-only job
            # repairs the media internally without exposing a pipeline error.
            body["media_plan_repair"] = {"error": str(exc),
                "previous_scene": body.get("scene"),
                "response_type": body.get("response_type")}
            body["response_type"] = "chat"
            body["scene"] = None
            return _complete_job_locked(job_id, body=body)
    finally:
        _job_settings.reset(token)


@app.route("/jobs/deliveries", methods=["GET"])
def delivery_status():
    with db_lock, get_db() as conn:
        rows = conn.execute("SELECT id,job_id,status,attempts,last_error FROM deliveries WHERE status != 'delivered' ORDER BY id").fetchall()
        return jsonify([dict(row) for row in rows])


@app.route("/jobs/deliveries/<int:delivery_id>/retry", methods=["POST"])
def retry_delivery(delivery_id):
    with db_lock, get_db() as conn:
        changed = conn.execute("UPDATE deliveries SET status='pending',attempts=0,next_attempt=0,last_error=NULL WHERE id=? AND status='failed'", (delivery_id,)).rowcount
        if not changed:
            return jsonify({"error": "delivery is not failed"}), 409
        return jsonify({"status": "ok"})


def _complete_job_locked(job_id, body=None):
    """Body of the former complete_job: runs entirely under db_lock.
    All Telegram sends go through _defer()."""
    if body is None:
        body = request.get_json(force=True)
    ok = body.get("ok", False)
    text = body.get("text", "")
    image_b64 = body.get("image_b64")
    video_b64 = body.get("video_b64")
    images_b64 = body.get("images_b64")  # list, for character_image_batch
    audios_b64 = body.get("audios_b64")  # list, for voice_sample_batch

    with db_lock, get_db() as conn:
        row = conn.execute(
            """SELECT j.chat_id,j.context_id,j.job_type,j.payload,j.status,j.lease_token,
                      (SELECT transport FROM contexts WHERE id=j.context_id) AS transport
               FROM jobs j WHERE j.id=?""", (job_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "unknown job"}), 404

        if row["lease_token"] != body.get("lease_token"):
            return jsonify({"error": "stale completion owner"}), 409

        if row["status"] in ("done", "failed"):
            # Already resolved once — a duplicate/late completion call (e.g. the
            # worker's own request timing out client-side after the server-side
            # work actually finished) should not overwrite a real result.
            print(f"[jobs] ignoring duplicate completion for job {job_id}, already {row['status']}")
            return jsonify({"status": "ignored", "reason": f"already {row['status']}"})

        chat_id = row["chat_id"]
        context_id = row["context_id"]
        is_web = row["transport"] == "web"
        job_type = row["job_type"]
        payload = json.loads(row["payload"])
        if not ok and job_type == "character_chat_intent":
            # Model/planning outages are internal retries, never a failed character
            # reply. Finish this attempt atomically with its replacement job.
            retry_payload = dict(payload)
            retry_payload["internal_retry_count"] = int(payload.get("internal_retry_count", 0)) + 1
            retry_payload["internal_retry_after"] = time.time() + min(60, 2 ** min(retry_payload["internal_retry_count"], 6))
            followup_job_id = enqueue_job(conn, chat_id, job_type, "light",
                retry_payload, priority=10, context_id=context_id)
            conn.execute("UPDATE jobs SET status='done',result=?,completed_at=? WHERE id=?",
                (json.dumps({"has_media": False, "text": "",
                             "followup_job_id": followup_job_id}),
                 datetime.utcnow().isoformat(), job_id))
            logging.warning("Character chat job %s scheduled internal retry %s", job_id, followup_job_id)
            return jsonify({"status": "ok", "followup_job_id": followup_job_id})
        artifact_id = None
        artifact_kind = None
        candidate_artifact_ids = []
        if ok and context_id is not None and (video_b64 or image_b64):
            artifact_kind = "video" if video_b64 else "image"
            encoded = video_b64 or image_b64
            media_bytes = base64.b64decode(encoded, validate=True)
            artifact_id = persist_artifact(
                conn, job_id=job_id, context_id=context_id,
                character_id=payload.get("character_id"), kind=artifact_kind,
                data=media_bytes,
                mime_type="video/mp4" if artifact_kind == "video" else "image/png",
                metadata={
                    "continuation_of_artifact_id": payload.get("continuation_artifact_id"),
                    "continuation_parent_job_id": payload.get("continuation_parent_job_id"),
                } if payload.get("continuation_artifact_id") else None,
            )
            message_id = payload.get("message_id") or payload.get("web_message_id")
            if message_id is not None:
                owned = conn.execute(
                    "SELECT 1 FROM messages WHERE id=? AND context_id=?",
                    (message_id, context_id),
                ).fetchone()
                if not owned:
                    raise ValueError("Media target message does not belong to this context")
            else:
                message_id = record_message(
                    conn, payload.get("character_id"), chat_id, "assistant",
                    payload.get("caption") or f"Created a {artifact_kind}.",
                    kind=artifact_kind, context_id=context_id,
                )
            conn.execute(
                "INSERT INTO message_artifacts(message_id,artifact_id) VALUES(?,?)",
                (message_id, artifact_id),
            )
        if ok and is_web and (images_b64 or audios_b64):
            candidate_kind="image" if images_b64 else "audio"
            candidate_mime="image/png" if candidate_kind=="image" else "audio/wav"
            for encoded in images_b64 or audios_b64:
                candidate_artifact_ids.append(persist_artifact(
                    conn,job_id=job_id,context_id=context_id,
                    character_id=payload.get("character_id"),kind=candidate_kind,
                    data=base64.b64decode(encoded,validate=True),mime_type=candidate_mime,
                ))

        conn.execute(
            "UPDATE jobs SET status = ?, result = ?, completed_at = ? WHERE id = ?",
            (
                "done" if ok else "failed",
                json.dumps({"has_image": bool(image_b64 or images_b64), "has_media": bool(video_b64 or image_b64), "text": text,
                            "artifact_id": artifact_id, "artifact_kind": artifact_kind,
                            "candidate_artifact_ids": candidate_artifact_ids,
                            "media_plan_error": body.get("media_plan_error")}),
                datetime.utcnow().isoformat(),
                job_id,
            ),
        )

        if not ok:
            if not is_web:
                _defer(conn, job_id, "msg", chat_id, f"⚠️ Job failed: {text}")
            # Reset any in-flight session state the (now-failed) job had latched,
            # so the busy-guard (get_in_progress_session) stops blocking commands.
            # Only the busy states — not active chats or awaiting_* prompt flows.
            busy = ("profile_generating", "image_queued",
                    "promoting_reference", "voice_generating",
                    "promoting_voice")
            if context_id is not None:
                conn.execute("DELETE FROM sessions WHERE context_id=? AND state IN (%s)" % ",".join("?"*len(busy)),(context_id,*busy))
            else:
                conn.execute("DELETE FROM sessions WHERE chat_id=? AND state IN (%s)" % ",".join("?"*len(busy)),(chat_id,*busy))
            conn.commit()
            return jsonify({"status": "ok"})

        if job_type == "promote_scene_anchor":
            if payload.get("scene_id"):
                conn.execute("""INSERT INTO scene_assets(scene_id,aspect_mode,anchor_artifact_id,anchor_reference_image)
                    VALUES(?,?,?,?) ON CONFLICT(scene_id,aspect_mode) DO UPDATE SET
                    anchor_artifact_id=excluded.anchor_artifact_id,anchor_reference_image=excluded.anchor_reference_image,
                    revision=scene_assets.revision+1,updated_at=CURRENT_TIMESTAMP""",
                    (payload["scene_id"],payload["aspect_mode"],payload["artifact_id"],text))
            else:
                conn.execute("""UPDATE character_scenes
                    SET anchor_artifact_id=?,anchor_reference_image=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP
                    WHERE character_id=?""",(payload["artifact_id"],text,payload["character_id"]))
            if not is_web:
                _defer(conn,job_id,"msg",chat_id,"Scene anchor approved. Future single-character images and videos will branch from it.")
            return jsonify({"status":"ok","artifact_id":payload["artifact_id"]})

        if job_type == "list_models":
            models = body.get("models", [])
            if not models:
                _defer(conn, job_id, "msg", chat_id, "Couldn't reach Ollama or no models installed.")
                return jsonify({"status": "ok"})
            cur = conn.execute(
                "INSERT INTO sessions (character_id, chat_id, state, pending_data) "
                "VALUES (NULL, ?, 'awaiting_model_choice', ?)",
                (chat_id, json.dumps(models)),
            )
            current_model = get_setting(conn, "current_model")
            listing = "\n".join(
                f"{i}. {m}" + (" ⬅ in use" if m == current_model else "")
                for i, m in enumerate(models, start=1)
            )
            header = "Available models:"
            if current_model and current_model in models:
                header += f"\nCurrently: {current_model}"
            _defer(conn, job_id, "msg", chat_id, f"{header}\n{listing}\n\nReply with a number to switch.")
            return jsonify({"status": "ok"})

        if job_type == "character_chat_intent":
            response_type = body.get("response_type", "chat")
            scene = body.get("scene")
            scene_context = str(payload.get("scene_context") or "").strip()
            reply_text = text  # worker puts the drafted in-character reply here
            # Belt-and-suspenders echo check (job 659 bug): the worker already
            # guards this, but a future model could still mirror the user's
            # message into reply_text. Never deliver a mirror — substitute a
            # clean line and drop the media (worker will have sent nothing yet).
            def _norm(s):
                return "".join(ch for ch in str(s or "").lower() if ch.isalnum())
            if _norm(reply_text) == _norm(payload.get("message", "")):
                print(f"[complete] job {job_id}: reply echoed user message — substituting fallback")
                reply_text = "Mm... my mind went blank for a second there. Say that again? ❤"
                response_type, scene = "chat", None
            # The text the user reads WITH the media: use the in-character reply_text
            # (the answer to the user's message), not a separate "about sending" caption.
            # reply_text is drafted by the classifier to respond to the user's exact
            # words; we always show it for both chat and media so it feels like a live
            # reply to the question rather than a generic away-message. (caption is kept
            # only as a fallback if reply_text ever comes back empty.)
            caption = reply_text or body.get("caption")
            # Short, emotional spoken line the character SAYS IN the video clip.
            # Only use a classifier-drafted video_line — do NOT fall back to the full
            # reply_text: reply_text is the caption (descriptive prose about the video),
            # and collapsing it into the spoken line makes H3 recite the whole caption
            # verbatim (job 714/716: spoken dialog == caption). If no distinct line was
            # drafted, leave it None so the worker extracts one or H3 invents a short
            # in-character vocalization instead.
            video_line = body.get("video_line")
            # Persist the character's reply into durable history (VPS = single writer).
            kind = {"image": "image", "video": "video"}.get(response_type, "chat")
            reply_message_id = payload.get("repair_reply_message_id")
            if reply_message_id is None:
                reply_message_id = record_message(
                    conn, payload["character_id"], chat_id, "assistant", reply_text,
                    kind=kind, context_id=context_id,
                )
            followup_job_id = None
            if body.get("media_plan_repair"):
                retry_payload = dict(payload)
                retry_payload.update(drafted_reply=reply_text,
                    repair_reply_message_id=reply_message_id,
                    media_plan_repair=body["media_plan_repair"],
                    shared_visual_moment=body.get("shared_visual_moment") or payload.get("shared_visual_moment"))
                retry_payload["internal_retry_count"] = int(payload.get("internal_retry_count", 0)) + 1
                retry_payload["internal_retry_after"] = time.time() + min(60, 2 ** min(retry_payload["internal_retry_count"], 6))
                followup_job_id = enqueue_job(conn, chat_id,
                    "character_chat_intent", "light", retry_payload,
                    priority=10, context_id=context_id)

            if response_type == "image":
                active_char = conn.execute(
                    "SELECT * FROM characters WHERE id = ?", (payload["character_id"],)
                ).fetchone()
                # Guard: if the classifier asked for an image but left scene empty,
                # pick a VARIED identity-preserving pose so consecutive photos don't
                # all render as the same standing portrait.
                if not scene:
                    scene = random.choice([
                        "a playful close-up portrait, caught mid-laugh",
                        "a casual half-length shot, leaning against a wall with a knowing smile",
                        "a full-length pose, mid-stride like they just looked up and saw you",
                        "a relaxed seated pose, chin resting on one hand, eyes on the camera",
                        "a shoulder-up shot glancing away with a shy smile",
                        "a confident full-length stance, arms crossed, one eyebrow raised",
                    ])
                if scene_context:
                    scene = (f"Established overall world and continuity: {scene_context}. "
                             f"Current planned image (including reply-supported related-room transitions, takes precedence): {scene}")
                if payload.get("scene_reference_image"):
                    scene = ("Use the approved scene anchor as the visual baseline. Preserve its character identity, "
                             "overall world, clothing, and required props unless the latest request changes them. Related-room transitions supported by the reply are allowed. "
                             "Pose, action, framing, static camera angle and gaze follow the planned image below "
                             "and may differ from the anchor while preserving requested action and scene facts: "
                             f"{scene}")
                instruction = validate_followup_instruction(active_char["concept"], scene, payload.get("message", ""), reply_text,
                    (payload.get("settings_snapshot") or {}).get("content_mode") or get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE)
                followup_job_id = enqueue_job(
                    conn, chat_id, "character_image_edit", "heavy",
                    {
                        "prompt": instruction,
                        "visual_state": body.get("visual_state") or {"description": str(body.get("scene") or "")},
                        "latest_request": payload.get("message", ""),
                        "character_id": active_char["id"],
                        "reference_image": payload.get("scene_reference_image") or active_char["base_image_path"],
                        "caption": caption,
                        "message_id": reply_message_id,
                        "scene_context": scene_context,
                        "scene_revision": payload.get("scene_revision", 0),
                    },
                    context_id=context_id,
                )
            elif response_type == "video":
                active_char = conn.execute(
                    "SELECT * FROM characters WHERE id = ?", (payload["character_id"],)
                ).fetchone()
                # Guard: if a video was requested but the scene came back empty,
                # pick a VARIED natural motion so clips don't all look identical.
                if not scene:
                    scene = random.choice([
                        "leans in close and speaks directly to the camera",
                        "tilts their head with a slow smile, then bites their lip",
                        "runs a hand through their hair while holding your gaze",
                        "shifts closer, voice dropping as they hold eye contact",
                    ])
                if scene_context:
                    scene = (f"Maintain the established scene and its visible continuity: {scene_context}. "
                             f"Latest requested action (takes precedence): {scene}")
                if payload.get("scene_reference_image"):
                    scene = ("Use the approved scene anchor as the visual baseline. Preserve its character identity, "
                             "environment, lighting, clothing, props, and overall composition unless the latest request "
                             f"explicitly changes one. Change only what the latest request requires: {scene}")
                scene_instruction = validate_followup_instruction(active_char["concept"], scene, payload.get("message", ""), reply_text,
                    (payload.get("settings_snapshot") or {}).get("content_mode") or get_setting(conn, "content_mode") or DEFAULT_CONTENT_MODE)
                followup_job_id = enqueue_job(
                    conn, chat_id, "character_video", "heavy",
                    {
                        "scene_prompt": scene_instruction,
                        "motion_prompt": scene,
                        "reference_image": payload.get("scene_reference_image") or active_char["base_image_path"],
                        "concept": active_char["concept"],
                        "character_name": active_char["name"],
                        "voice_audio": active_char["voice_sample_path"] or None,
                        "spoken_line": video_line,  # short emotional line the char SAYS in the clip
                        "caption": caption,  # message ABOUT the video, separate from spoken line
                        "message_id": reply_message_id,
                        "scene_context": scene_context,
                        "scene_revision": payload.get("scene_revision", 0),
                    },
                    priority=10, context_id=context_id,
                )
            else:
                if not is_web and not payload.get("repair_reply_message_id"):
                    _defer(conn, job_id, "msg", chat_id, reply_text)
            if is_web and followup_job_id is not None:
                conn.execute(
                    "UPDATE jobs SET result=? WHERE id=?",
                    (json.dumps({"has_media": False, "text": reply_text,
                                 "followup_job_id": followup_job_id}), job_id),
                )
            return jsonify({"status": "ok", "followup_job_id": followup_job_id})

        if job_type == "promote_reference":
            character_id = payload["character_id"]
            new_filename = text  # worker returns the new input-folder filename in `text`
            conn.execute(
                "UPDATE characters SET base_image_path = ? WHERE id = ?", (new_filename, character_id)
            )
            session = conn.execute(
                "SELECT * FROM sessions WHERE character_id = ? AND state = 'promoting_reference'",
                (character_id,),
            ).fetchone()
            # Voice step comes right after the image is locked: generate 3 sample
            # clips so the user can pick the character's voice before going active.
            if session is None:
                # Session lost (failure-path cleanup raced a late completion, or a
                # stale requeue). Don't 500 — the image IS locked; tell the user
                # and stop the flow here instead of enqueueing into a dead session.
                print(f"[complete] promote_reference: no promoting_reference session for char {character_id}; skipping voice step")
                _defer(conn, job_id, "msg",
                    chat_id,
                    "Image locked, but the creation session was lost — voice setup was skipped. Use /newcharacter to restart or /characters to pick up.",
                )
                return jsonify({"status": "ok"})
            touch_session(conn, session["id"], state="voice_generating")
            char = conn.execute(
                "SELECT concept, name, personality_notes FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
            concept, char_name = char["concept"], char["name"]
            char_notes = char["personality_notes"] or ""
            gender_hint = infer_gender_hint(concept)
            profile = build_voice_profile(concept, gender_hint, char_notes)
            enqueue_job(
                conn, chat_id, "voice_sample_batch", "heavy",
                {
                    "character_id": character_id,
                    "prompt": make_voice_sample_line(char_name, concept),
                    "gender_hint": gender_hint,
                    # Descriptive-voice engines (qwen3) design a NEW voice from
                    # this text instead of picking a fixed speaker — so embed the
                    # character's own described gender, age, and objective acoustic
                    # attributes (pitch / pace / characteristic / vibe) plus a hard
                    # anti-cartoony clause. The worker builds 3 structurally-distinct
                    # descriptions from the profile.
                    "voice_profile": profile,
                    "voice_description": make_voice_description(
                        concept, gender_hint, char_name, char_notes
                    ),
                }, context_id=context_id,
            )
            if not is_web:
                _defer(conn, job_id, "msg",chat_id,"Image locked. Generating 3 voice samples now — pick your favorite in a moment.")
            return jsonify({"status": "ok"})

        if job_type == "voice_sample_batch":
            character_id = payload["character_id"]
            session = conn.execute(
                "SELECT * FROM sessions WHERE character_id = ? AND state = 'voice_generating'",
                (character_id,),
            ).fetchone()
            audio_files = body.get("audio_files", [])  # list of {filename, subfolder}
            if session is None:
                # Session lost (e.g. a failed sibling job triggered busy-state
                # cleanup). Deliver the audio anyway — the clips are real — but
                # don't crash; without a session the choice can't be wired up.
                print(f"[complete] voice_sample_batch: no voice_generating session for char {character_id}; delivering audio without picker")
                for i, b64 in enumerate(audios_b64, start=1):
                    _defer(conn, job_id, "audio", chat_id, base64.b64decode(b64), caption=f"Option {i}")
                _defer(conn, job_id, "msg",
                    chat_id,
                    "Voice samples arrived but their session was lost — reply /newcharacter to redo, or pick a voice later via /workflow.",
                )
                return jsonify({"status": "ok"})
            touch_session(
                conn, session["id"], state="awaiting_voice_choice",
                pending_data=json.dumps(audio_files),
            )
            if not is_web:
                for i, b64 in enumerate(audios_b64, start=1):
                    spk = audio_files[i - 1].get("speaker", "") if i - 1 < len(audio_files) else ""
                    cap = f"Option {i}" + (f" ({spk})" if spk else "")
                    _defer(conn, job_id, "audio", chat_id, base64.b64decode(b64), caption=cap)
            name = conn.execute("SELECT name FROM characters WHERE id = ?", (character_id,)).fetchone()["name"]
            if not is_web:
                _defer(conn, job_id, "msg",chat_id,f"Voice samples for {name}. Reply with a number 1-{len(audios_b64)} to pick one, or 0 to reroll.")
            return jsonify({"status": "ok"})

        if job_type == "promote_voice":
            character_id = payload["character_id"]
            new_filename = text  # worker returns the new input-folder filename in `text`
            conn.execute(
                "UPDATE characters SET voice_sample_path = ? WHERE id = ?", (new_filename, character_id)
            )
            session = conn.execute(
                "SELECT * FROM sessions WHERE character_id = ? AND state = 'promoting_voice'",
                (character_id,),
            ).fetchone()
            if session is None:
                # The voice IS promoted (UPDATE above already ran); just skip the
                # session transition instead of crashing on a missing row.
                print(f"[complete] promote_voice: no promoting_voice session for char {character_id}; promotion kept, session skipped")
                name = conn.execute("SELECT name FROM characters WHERE id = ?", (character_id,)).fetchone()["name"]
                _defer(conn, job_id, "msg",
                    chat_id,
                    f"{name}'s voice was set. (Creation session had already ended.)",
                )
                return jsonify({"status": "ok"})
            touch_session(conn, session["id"], state="active", pending_data=None)
            set_shared_active_character(conn,character_id)
            if chat_id:
                telegram_context=ensure_transport_context(conn,"telegram",chat_id)
                conn.execute("""INSERT INTO context_selections(context_id,character_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                  ON CONFLICT(context_id) DO UPDATE SET character_id=excluded.character_id,updated_at=CURRENT_TIMESTAMP""",(telegram_context,character_id))
            name = conn.execute("SELECT name FROM characters WHERE id = ?", (character_id,)).fetchone()["name"]
            if not is_web:
                _defer(conn, job_id, "msg",chat_id,f"{name} is ready. Use /image <description> or /video <description> to bring them to life.")
            return jsonify({"status": "ok"})

        if job_type == "edit_profile_generate":
            character_id = payload["character_id"]
            conn.execute("UPDATE characters SET system_prompt = ? WHERE id = ?", (text, character_id))
            if not is_web:
                _defer(conn, job_id, "msg", chat_id, f"Profile updated for {payload.get('name', 'the character')}.")
            return jsonify({"status": "ok"})

        if job_type == "profile_generate":
            character_id = payload["character_id"]
            conn.execute("UPDATE characters SET system_prompt = ? WHERE id = ?", (text, character_id))
            session = conn.execute(
                "SELECT * FROM sessions WHERE character_id = ? AND state = 'profile_generating'",
                (character_id,),
            ).fetchone()
            if session is None:
                # Profile text IS saved (UPDATE above ran); skip the image-batch
                # chain rather than crash — user can regenerate via /editcharacter.
                print(f"[complete] profile_generate: no profile_generating session for char {character_id}; profile saved, image step skipped")
                _defer(conn, job_id, "msg", chat_id, "Profile was generated and saved, but its session ended early. Use /characters → edit to continue.")
                return jsonify({"status": "ok"})
            touch_session(conn, session["id"], state="image_queued")
            concept = conn.execute(
                "SELECT concept, personality_notes FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
            enqueue_job(
                conn, chat_id, "character_image_batch", "heavy",
                {"character_id": character_id, "prompt": concept["concept"], "personality_notes": concept["personality_notes"]}, context_id=context_id,
            )
            if not is_web:
                _defer(conn, job_id, "msg", chat_id, "Profile set. Generating look candidates now...")
            return jsonify({"status": "ok"})

        if job_type == "character_image_batch":
            character_id = payload["character_id"]
            session = conn.execute(
                "SELECT * FROM sessions WHERE character_id = ? AND state = 'image_queued'",
                (character_id,),
            ).fetchone()
            candidate_filenames = body.get("filenames", [])
            if session is None:
                # Candidates ARE real; deliver them without a picker instead of 500ing.
                print(f"[complete] character_image_batch: no image_queued session for char {character_id}; delivering images without picker")
                for i, b64 in enumerate(images_b64, start=1):
                    _defer(conn, job_id, "photo", chat_id, base64.b64decode(b64), caption=f"Option {i}")
                _defer(conn, job_id, "msg", chat_id, "Look candidates arrived but their session ended — reply /newcharacter to restart, or /characters to continue.")
                return jsonify({"status": "ok"})
            touch_session(
                conn, session["id"], state="awaiting_choice",
                pending_data=json.dumps(candidate_filenames),
            )
            # Deferred delivery: photos are queued and sent after db_lock
            # release. Per-option success tracking isn't available in the
            # deferred model — send_telegram_photo still retries once and
            # logs failures; the picker prompt goes out unconditionally.
            if not is_web:
                for i, b64 in enumerate(images_b64, start=1):
                    _defer(conn, job_id, "photo", chat_id, base64.b64decode(b64), caption=f"Option {i}")
                _defer(conn, job_id, "msg", chat_id, f"Reply with a number 1-{len(images_b64)} to pick one, or 0 to reroll.")
            return jsonify({"status": "ok"})

        # Default paths: plain chat, the generic /image command, or /video
        if is_web:
            return jsonify({"status": "ok", "artifact_id": artifact_id})
        video_b64 = body.get("video_b64")
        if video_b64:
            controls={"inline_keyboard":[[{"text":"Continue video","callback_data":f"continue:{artifact_id}"}]]} if artifact_id else None
            _defer(conn, job_id, "video", chat_id, base64.b64decode(video_b64),
                   caption=payload.get("caption"),reply_markup=controls)
        elif image_b64:
            _defer(conn, job_id, "photo", chat_id, base64.b64decode(image_b64), caption=payload.get("caption"))
        else:
            _defer(conn, job_id, "msg", chat_id, text)
        # Persist the bare llm_chat reply into its per-chat durable history.
        if job_type == "llm_chat" and not (video_b64 or image_b64):
            record_message(conn, None, chat_id, "assistant", text, kind="chat")
        return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("[vps] imports complete; initializing", flush=True)
    # Global exception hook — log any unhandled exception to vps.log
    # so the VPS never dies without a traceback appearing in the log.
    def _excepthook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        msg = f"[VPS FATAL] Unhandled exception: {tb}"
        print(msg, flush=True)
        # Also write directly to vps-internal.log as a fallback
        try:
            import os as _os
            with open(_os.path.join(os.path.dirname(__file__), "..", "logs", "vps-internal.log"), "a") as _f:
                _f.write(msg + "\n")
        except OSError:
            pass
    sys.excepthook = _excepthook
    threading.excepthook = lambda args: _excepthook(args.exc_type, args.exc_value, args.exc_traceback)

    _instance = InstanceLock("vps-db:" + os.path.normcase(os.path.realpath(DB_PATH)))
    if not _instance.acquire():
        raise SystemExit("Another VPS owns this database")
    if TELEGRAM_ENABLED:
        if not TELEGRAM_BOT_TOKEN:
            raise SystemExit("TELEGRAM_BOT_TOKEN is missing")
        _poll_lock = InstanceLock("telegram:" + TELEGRAM_BOT_TOKEN)
        if not _poll_lock.acquire():
            raise SystemExit("Another instance already polls this Telegram bot")
    _heartbeat = Heartbeat("vps").start()
    print("[vps] heartbeat started; initializing database", flush=True)
    init_db()
    print("[vps] database ready", flush=True)
    if TELEGRAM_ENABLED:
        print("[vps] registering Telegram commands", flush=True)
        setup_telegram_commands()
        _delivery = DeliveryWorker(get_db, db_lock, {
            "msg": send_telegram_message, "photo": send_telegram_photo,
            "audio": send_telegram_audio, "video": send_telegram_video,
            "character_grid": send_character_grid,
            "command_menu": send_telegram_command_menu}).start()
        threading.Thread(target=telegram_listener, daemon=True).start()
    # Local-only by default; trusted-network access is an explicit configuration choice.
    api_host = os.environ.get("API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # Use Waitress (production WSGI) instead of Flask's dev server — avoids
    # send_response recursion, single-threading issues, and long-poll timeouts.
    from waitress import serve
    faulthandler.cancel_dump_traceback_later()
    print(f"[vps] serving HTTP on {api_host}:{API_PORT}", flush=True)
    serve(app, host=api_host, port=API_PORT, threads=8)
