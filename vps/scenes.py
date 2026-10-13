"""Reusable, character-independent scene state and legacy migration."""

VALID_ASPECTS = ("vertical", "cinematic")


def migrate_reusable_scenes(conn):
    """Copy legacy per-character scenes into the RC3 scene library once."""
    legacy = conn.execute(
        """SELECT cs.*, COALESCE(c.name, 'Character') character_name
           FROM character_scenes cs LEFT JOIN characters c ON c.id=cs.character_id"""
    ).fetchall()
    active_character = conn.execute("SELECT value FROM settings WHERE key='active_character_id'").fetchone()
    active_character_id = int(active_character[0]) if active_character and str(active_character[0]).isdigit() else None
    active_scene = conn.execute("SELECT value FROM settings WHERE key='active_scene_id'").fetchone()

    for row in legacy:
        conn.execute(
            """INSERT OR IGNORE INTO scenes(name,description,revision,legacy_character_id,updated_at)
               VALUES(?,?,?,?,?)""",
            (f"{row['character_name']} scene", row["description"], row["revision"],
             row["character_id"], row["updated_at"]),
        )
        scene = conn.execute(
            "SELECT id FROM scenes WHERE legacy_character_id=?", (row["character_id"],)
        ).fetchone()
        # Legacy anchors contain the owning character. Keep them in
        # character_scenes for rollback, but never promote them into the RC3
        # character-free environment library.
        if not active_scene and row["enabled"] and row["character_id"] == active_character_id:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('active_scene_id',?)",
                (str(scene["id"]),),
            )
            active_scene = (str(scene["id"]),)


def scene_view(conn, scene_id, aspect_mode=None):
    if not scene_id:
        return None
    scene = conn.execute(
        "SELECT id,name,description,revision,created_at,updated_at FROM scenes WHERE id=?", (scene_id,)
    ).fetchone()
    if not scene:
        return None
    assets = {
        row["aspect_mode"]: {
            "artifact_id": row["anchor_artifact_id"],
            "reference_image": row["anchor_reference_image"],
            "ready": bool(row["anchor_reference_image"]),
            "revision": row["revision"],
        }
        for row in conn.execute(
            "SELECT * FROM scene_assets WHERE scene_id=? ORDER BY aspect_mode", (scene_id,)
        )
    }
    result = dict(scene)
    result["assets"] = assets
    if aspect_mode:
        exact = assets.get(aspect_mode)
        if exact and exact.get("ready"):
            result["active_asset"] = exact
            result["active_asset_aspect"] = aspect_mode
            result["active_asset_fallback"] = False
        else:
            # Continuity is more important than silently dropping the scene
            # whenever the user switches aspect ratios. Krea can reframe an
            # approved anchor; without one, identity-photo backgrounds become
            # the only visual environment and can take over the result.
            fallback = next(
                ((mode, asset) for mode, asset in assets.items() if asset.get("ready")),
                None,
            )
            result["active_asset"] = fallback[1] if fallback else exact
            result["active_asset_aspect"] = fallback[0] if fallback else aspect_mode
            result["active_asset_fallback"] = bool(fallback)
    return result


def active_scene_view(conn, aspect_mode=None):
    row = conn.execute("SELECT value FROM settings WHERE key='active_scene_id'").fetchone()
    return scene_view(conn, int(row[0]), aspect_mode) if row and str(row[0]).isdigit() else None
