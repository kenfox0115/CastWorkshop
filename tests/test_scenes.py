import sqlite3
import unittest
from pathlib import Path

from vps.scenes import active_scene_view, migrate_reusable_scenes, scene_view


class ReusableSceneTests(unittest.TestCase):
    def connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        schema = Path(__file__).resolve().parents[1] / "vps" / "schema.sql"
        conn.executescript(schema.read_text(encoding="utf-8"))
        return conn

    def test_legacy_scene_migration_is_idempotent_without_reusing_character_anchor(self):
        conn = self.connection()
        character_id = conn.execute("INSERT INTO characters(name) VALUES('Mara')").lastrowid
        conn.execute("INSERT INTO settings(key,value) VALUES('active_character_id',?)", (str(character_id),))
        conn.execute("INSERT INTO settings(key,value) VALUES('aspect_mode','cinematic')")
        conn.execute(
            """INSERT INTO character_scenes
               (character_id,enabled,description,anchor_artifact_id,anchor_reference_image,revision)
               VALUES(?,1,'A quiet observatory',7,'legacy-anchor.png',4)""",
            (character_id,),
        )

        migrate_reusable_scenes(conn)
        migrate_reusable_scenes(conn)

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0], 1)
        scene = active_scene_view(conn, "cinematic")
        self.assertEqual(scene["name"], "Mara scene")
        self.assertEqual(scene["description"], "A quiet observatory")
        self.assertIsNone(scene["active_asset"])
        legacy = conn.execute("SELECT anchor_reference_image FROM character_scenes WHERE character_id=?", (character_id,)).fetchone()
        self.assertEqual(legacy["anchor_reference_image"], "legacy-anchor.png")

    def test_scene_view_keeps_aspect_assets_separate(self):
        conn = self.connection()
        scene_id = conn.execute(
            "INSERT INTO scenes(name,description) VALUES('Loft','An empty industrial loft')"
        ).lastrowid
        conn.execute(
            "INSERT INTO scene_assets(scene_id,aspect_mode,anchor_reference_image) VALUES(?,?,'portrait.png')",
            (scene_id, "vertical"),
        )
        conn.execute(
            "INSERT INTO scene_assets(scene_id,aspect_mode,anchor_reference_image) VALUES(?,?,'wide.png')",
            (scene_id, "cinematic"),
        )
        scene = scene_view(conn, scene_id, "vertical")
        self.assertEqual(scene["active_asset"]["reference_image"], "portrait.png")
        self.assertEqual(scene["assets"]["cinematic"]["reference_image"], "wide.png")
        self.assertFalse(scene["active_asset_fallback"])

    def test_scene_view_reframes_other_approved_aspect_instead_of_dropping_anchor(self):
        conn = self.connection()
        scene_id = conn.execute(
            "INSERT INTO scenes(name,description) VALUES('Club','A neon dance floor')"
        ).lastrowid
        conn.execute(
            "INSERT INTO scene_assets(scene_id,aspect_mode,anchor_reference_image) VALUES(?,?,'portrait.png')",
            (scene_id, "vertical"),
        )
        scene = scene_view(conn, scene_id, "cinematic")
        self.assertEqual(scene["active_asset"]["reference_image"], "portrait.png")
        self.assertEqual(scene["active_asset_aspect"], "vertical")
        self.assertTrue(scene["active_asset_fallback"])
        self.assertNotIn("cinematic", scene["assets"])


if __name__ == "__main__":
    unittest.main()
