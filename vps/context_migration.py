"""Idempotent transport-context migration for Telegram and local web."""
def migrate_contexts(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS contexts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transport TEXT NOT NULL CHECK (transport IN ('telegram','web')),
        external_id TEXT NOT NULL,
        display_name TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_active_at TEXT,
        UNIQUE(transport,external_id)
    );
    CREATE TABLE IF NOT EXISTS context_selections (
        context_id INTEGER PRIMARY KEY,
        character_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(context_id) REFERENCES contexts(id),
        FOREIGN KEY(character_id) REFERENCES characters(id)
    );
    CREATE TABLE IF NOT EXISTS artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        context_id INTEGER NOT NULL,
        character_id INTEGER,
        kind TEXT NOT NULL CHECK (kind IN ('image','video','audio','thumbnail')),
        relative_path TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        byte_size INTEGER,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS message_artifacts (
        message_id INTEGER NOT NULL,
        artifact_id INTEGER NOT NULL,
        display_order INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(message_id,artifact_id)
    );
    CREATE INDEX IF NOT EXISTS idx_artifacts_context ON artifacts(context_id,character_id,id);
    """)
    for table in ("sessions","messages","jobs","deliveries"):
        columns={row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "context_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN context_id INTEGER")
    chat_ids=set()
    for table in ("sessions","messages","jobs","deliveries"):
        chat_ids.update(row[0] for row in conn.execute(
            f"SELECT DISTINCT chat_id FROM {table} WHERE chat_id IS NOT NULL AND chat_id != 0"
        ))
    for chat_id in chat_ids:
        conn.execute("INSERT OR IGNORE INTO contexts(transport,external_id) VALUES('telegram',?)",(str(chat_id),))
    for table in ("sessions","messages","jobs","deliveries"):
        conn.execute(f"""UPDATE {table} SET context_id=(
            SELECT id FROM contexts WHERE transport='telegram' AND external_id=CAST({table}.chat_id AS TEXT)
        ) WHERE context_id IS NULL AND chat_id IS NOT NULL AND chat_id != 0""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_context ON messages(context_id,character_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_character_order ON messages(character_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_context ON jobs(context_id,id)")
