-- Current schema. app.init_db migrates existing pre-0.9.7 job tables.

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    context_id INTEGER,
    job_type TEXT NOT NULL DEFAULT 'llm_chat',
    weight TEXT NOT NULL DEFAULT 'light',  -- 'light' | 'heavy' — gates release during peak hours
    payload TEXT NOT NULL,           -- JSON: {"prompt": "..."}
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | dispatched | done | failed
    result TEXT,                     -- JSON: {"text": "..."} once complete
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    dispatched_at DATETIME,
    completed_at DATETIME,
    priority INTEGER NOT NULL DEFAULT 5,
    lease_token TEXT,
    lease_expires_at REAL,
    worker_id TEXT,
    progress_json TEXT
);

-- Character state machine (section 3.3 of the architecture doc).
-- State sequence used here (renamed slightly from the doc's own listing,
-- which had 'awaiting_image_choice' ordered before any images exist):
--   awaiting_concept -> awaiting_personality_notes -> profile_generating
--   -> image_queued -> awaiting_choice -> active
-- reference_set_generating / audio_profile_capture are defined but not
-- wired to real jobs yet — they need their own exported ComfyUI workflows,
-- same as k2_txt2img_api.json was needed for the base image batch.

CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    concept TEXT,                     -- user's description of the character's look
    personality_notes TEXT,
    system_prompt TEXT,               -- LLM-generated, from profile_generating
    base_image_path TEXT,             -- ComfyUI filename of the chosen candidate
    identity_tier TEXT DEFAULT 'basic',  -- 'basic' | 'trained'
    voice_sample_path TEXT,           -- promoted MOSS voice clip in ComfyUI /input
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER,
    chat_id INTEGER NOT NULL,
    context_id INTEGER,
    state TEXT NOT NULL,
    pending_data TEXT,                 -- JSON scratch space (e.g. candidate list)
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (character_id) REFERENCES characters(id)
);

-- Simple key-value store, currently just used for the active Ollama model
-- selected via /model, so it survives worker restarts without an env var edit.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transport TEXT NOT NULL CHECK (transport IN ('telegram', 'web')),
    external_id TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_active_at TEXT,
    UNIQUE (transport, external_id)
);

CREATE TABLE IF NOT EXISTS context_selections (
    context_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (context_id) REFERENCES contexts(id),
    FOREIGN KEY (character_id) REFERENCES characters(id)
);

CREATE TABLE IF NOT EXISTS character_scenes (
    character_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    description TEXT NOT NULL DEFAULT '',
    anchor_artifact_id INTEGER,
    anchor_reference_image TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
    FOREIGN KEY (anchor_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    context_id INTEGER NOT NULL,
    character_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video', 'audio', 'thumbnail')),
    relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_artifacts_context ON artifacts(context_id, character_id, id);

-- RC3 reusable scenes are independent of the cast. Anchors are stored per
-- aspect because one composition cannot reliably serve both portrait and 16:9.
CREATE TABLE IF NOT EXISTS scenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    legacy_character_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scene_assets (
    scene_id INTEGER NOT NULL,
    aspect_mode TEXT NOT NULL CHECK (aspect_mode IN ('vertical', 'cinematic')),
    anchor_artifact_id INTEGER,
    anchor_reference_image TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scene_id, aspect_mode),
    FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE,
    FOREIGN KEY (anchor_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS message_artifacts (
    message_id INTEGER NOT NULL,
    artifact_id INTEGER NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, artifact_id)
);

-- Durable per-character conversation history (added for chat continuity).
-- Written by the VPS (the single DB owner); read at enqueue time so the
-- worker's Ollama call can see recent context across restarts.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER,             -- NULL for the bare llm_chat fallback (keyed by chat_id only)
    chat_id INTEGER NOT NULL,
    context_id INTEGER,
    role TEXT NOT NULL,               -- 'user' | 'assistant'
    kind TEXT,                        -- 'chat' | 'image' | 'video' | NULL for media context
    content TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (character_id) REFERENCES characters(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_char ON messages(character_id, chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_character_order ON messages(character_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_fallback ON messages(chat_id, created_at);



CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    chat_id INTEGER NOT NULL,
    context_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    lease_until REAL,
    last_error TEXT,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries(status, next_attempt);
