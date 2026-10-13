import json
import unittest
from unittest.mock import patch
import test_delivery
api = test_delivery.api

class CommandJobTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    tearDown = test_delivery.DeliveryTests.tearDown

    def test_fresh_database_defaults_to_sfw(self):
        with api.get_db() as conn:
            settings = api.capture_job_settings(conn)
            self.assertEqual(settings["content_mode"], "sfw")
            self.assertEqual(settings["workflows"]["image"], "k2_ref_sfw")

    def test_snofs_workflow_is_classified_as_nsfw(self):
        self.assertEqual(
            api._infer_workflow_mode("Krea2_turbo_SNOFS.json"),
            "nsfw",
        )

    def test_debug_menu_is_per_chat_and_toggles(self):
        api.handle_message(101, "/debug")
        with api.get_db() as conn:
            self.assertEqual(api.get_setting(conn, "debug_menu_101"), "on")
            self.assertIsNone(api.get_setting(conn, "debug_menu_202"))
            rows = conn.execute(
                "SELECT kind, payload FROM deliveries WHERE chat_id=101 ORDER BY id"
            ).fetchall()
            self.assertEqual([row["kind"] for row in rows], ["command_menu", "msg"])
            self.assertIn('"value": "debug"', rows[0]["payload"])

        api.handle_message(101, "/debug")
        with api.get_db() as conn:
            self.assertEqual(api.get_setting(conn, "debug_menu_101"), "off")
            last = conn.execute(
                "SELECT payload FROM deliveries WHERE chat_id=101 AND kind='command_menu' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIn('"value": "everyday"', last["payload"])

    def test_everyday_menu_order_and_debug_commands(self):
        self.assertEqual(
            [item["command"] for item in api.EVERYDAY_COMMANDS],
            ["characters", "newcharacter", "image", "multiimage", "video", "scene", "multivideo", "edit",
             "mode", "aspect", "videos", "delete", "debug"],
        )
        self.assertEqual(
            [item["command"] for item in api.DEBUG_COMMANDS],
            ["status", "retry", "model", "workflow", "endpoints"],
        )

    def test_commands_commit_replies_without_network(self):
        with patch.object(api.requests, "post", side_effect=AssertionError("network")), patch.object(api.requests, "get", side_effect=AssertionError("network")):
            api.handle_message(1, "/newcharacter")
            api.handle_message(1, "/status")
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT state FROM sessions").fetchone()[0], "awaiting_name")

    def test_scene_command_applies_reusable_scene_to_telegram_media(self):
        with api.get_db() as conn:
            character=conn.execute("""INSERT INTO characters(name,concept,system_prompt,base_image_path,voice_sample_path)
                VALUES('Avery','adult Avery','Be conversational.','avery.png','avery.wav')""").lastrowid
            conn.execute("INSERT INTO sessions(character_id,chat_id,state) VALUES(?,1,'active')",(character,))
            api.set_shared_active_character(conn,character)
        api.handle_message(1,"/scene set Rainy rooftop at night; red coat")
        api.handle_message(1,"/image waves at the camera")
        with api.get_db() as conn:
            scene=conn.execute("SELECT * FROM scenes ORDER BY id DESC LIMIT 1").fetchone()
            active_scene_id=api.get_setting(conn,"active_scene_id")
            job=conn.execute("SELECT job_type,payload FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            payload=json.loads(job["payload"])
        self.assertEqual(active_scene_id,str(scene["id"]))
        self.assertEqual(job["job_type"],"character_image_edit")
        self.assertIn("Rainy rooftop",payload["prompt"])
        self.assertEqual(payload["reference_image"],"avery.png")
        self.assertEqual(payload["scene_id"],scene["id"])

    def test_scene_anchor_command_queues_character_free_candidate(self):
        with api.get_db() as conn:
            character=conn.execute("""INSERT INTO characters(name,concept,base_image_path)
                VALUES('Avery','adult Avery','avery.png')""").lastrowid
            conn.execute("INSERT INTO sessions(character_id,chat_id,state) VALUES(?,1,'active')",(character,))
            api.set_shared_active_character(conn,character)
        api.handle_message(1,"/scene set Warm library with green walls")
        api.handle_message(1,"/scene anchor")
        with api.get_db() as conn:
            job=conn.execute("SELECT job_type,payload FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            payload=json.loads(job["payload"])
        self.assertEqual(job["job_type"],"comfy_image_batch")
        self.assertTrue(payload["scene_anchor_candidate"])
        self.assertNotIn("reference_image",payload)
        self.assertIn("completely unoccupied",payload["prompt"])
        self.assertIn(payload["scene_aspect_mode"],("vertical","cinematic"))

    def test_cancel_aborts_multichar_prompt_without_enqueuing_job(self):
        with api.get_db() as conn:
            conn.execute(
                "INSERT INTO sessions(character_id,chat_id,state,pending_data) VALUES(NULL,?,?,?)",
                (1, "awaiting_multichar_scene", json.dumps({
                    "char_a": {"id": 10, "name": "A"},
                    "char_b": {"id": 11, "name": "B"},
                })),
            )
        api.handle_message(1, "/cancel@TestBot")
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM sessions WHERE state != 'active'").fetchone()[0], 0
            )
            reply = conn.execute(
                "SELECT payload FROM deliveries WHERE chat_id=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(json.loads(reply)["value"], "Cancelled.")

    def test_started_job_cancel_requires_job_id_confirmation(self):
        with api.get_db() as conn:
            job=api.enqueue_job(conn,1,"character_video","heavy",{})
        api.handle_message(1,"/cancel")
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=?",(job,)).fetchone()[0],"queued")
            prompt=json.loads(conn.execute("SELECT payload FROM deliveries ORDER BY id DESC LIMIT 1").fetchone()[0])["value"]
            self.assertIn(f"/cancel {job}",prompt)
        api.handle_message(1,f"/cancel {job}")
        with api.get_db() as conn:
            row=conn.execute("SELECT status,result FROM jobs WHERE id=?",(job,)).fetchone()
            self.assertEqual(row["status"],"failed")
            self.assertTrue(json.loads(row["result"])["cancelled"])

    def test_retry_requeues_latest_failed_job(self):
        with api.get_db() as conn:
            character=conn.execute("INSERT INTO characters(name,concept) VALUES('Avery','adult')").lastrowid
            old=api.enqueue_job(conn,1,"character_video","heavy",{"character_id":character})
            conn.execute("UPDATE jobs SET status='failed',result=? WHERE id=?",(json.dumps({"text":"boom"}),old))
        api.handle_message(1,"/retry")
        with api.get_db() as conn:
            new=conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            self.assertNotEqual(new["id"],old)
            self.assertEqual(json.loads(new["payload"])["retry_of_job_id"],old)

    def test_retry_restores_character_creation_session(self):
        with api.get_db() as conn:
            character=conn.execute("INSERT INTO characters(name,concept) VALUES('Avery','adult')").lastrowid
            old=api.enqueue_job(conn,1,"character_image_batch","heavy",{"character_id":character})
            conn.execute("UPDATE jobs SET status='failed',result=? WHERE id=?",(json.dumps({"text":"boom"}),old))
        api.handle_message(1,f"/retry {old}")
        with api.get_db() as conn:
            session=conn.execute("SELECT state FROM sessions WHERE chat_id=1 AND character_id=?",(character,)).fetchone()
            self.assertEqual(session["state"],"image_queued")

    def test_video_callback_queues_single_character_continuation(self):
        with api.get_db() as conn:
            character=conn.execute("INSERT INTO characters(name,concept,base_image_path,voice_sample_path) VALUES('Avery','adult','a.png','a.flac')").lastrowid
            context=api.ensure_transport_context(conn,"telegram",1)
            source=api.enqueue_job(conn,1,"character_video","heavy",{"character_id":character,"character_name":"Avery","reference_image":"a.png","voice_audio":"a.flac"})
            conn.execute("UPDATE jobs SET status='done' WHERE id=?",(source,))
            artifact=conn.execute("INSERT INTO artifacts(job_id,context_id,character_id,kind,relative_path,mime_type) VALUES(?,?,?,?,?,?)",
                (source,context,character,"video","source.mp4","video/mp4")).lastrowid
        api.handle_callback_query({"id":"callback-1","data":f"continue:{artifact}","message":{"chat":{"id":1}}})
        with api.get_db() as conn:
            self.assertEqual(api.get_in_progress_session(conn,1)["state"],"awaiting_video_continuation")
        api.handle_message(1,'Avery turns and says "Ready."')
        with api.get_db() as conn:
            continued=conn.execute("SELECT job_type,payload FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            payload=json.loads(continued["payload"])
            self.assertEqual(continued["job_type"],"character_video")
            self.assertEqual(payload["continuation_artifact_id"],artifact)
            self.assertEqual(payload["motion_prompt"],'Avery turns and says "Ready."')

    def test_video_callback_confirms_multi_character_speakers(self):
        with api.get_db() as conn:
            context=api.ensure_transport_context(conn,"telegram",1)
            source=api.enqueue_job(conn,1,"multi_character_video","heavy",{
                "char_a":{"id":4,"name":"Avery","reference_image":"a.png"},
                "char_b":{"id":5,"name":"Blake","reference_image":"b.png"},
            })
            conn.execute("UPDATE jobs SET status='done' WHERE id=?",(source,))
            artifact=conn.execute("INSERT INTO artifacts(job_id,context_id,kind,relative_path,mime_type) VALUES(?,?,?,?,?)",
                (source,context,"video","source.mp4","video/mp4")).lastrowid
        api.handle_callback_query({"id":"callback-2","data":f"continue:{artifact}","message":{"chat":{"id":1}}})
        api.handle_message(1,'She smiles and says "Ready?"')
        with api.get_db() as conn:
            session=api.get_in_progress_session(conn,1)
            self.assertEqual(session["state"],"awaiting_continuation_speakers")
        api.handle_message(1,"A")
        with api.get_db() as conn:
            continued=conn.execute("SELECT job_type,payload FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            payload=json.loads(continued["payload"])
            self.assertEqual(continued["job_type"],"multi_character_video")
            self.assertEqual(payload["dialogue_turns"],[{"speaker":"a","line":"Ready?"}])

    def test_multichar_scene_rejects_unselected_saved_character(self):
        with api.get_db() as conn:
            ids = {}
            for name in ("Lylah", "Kaida", "Carmen"):
                ids[name] = conn.execute(
                    "INSERT INTO characters(name,concept,base_image_path,voice_sample_path) "
                    "VALUES(?,?,?,?)", (name, "adult", name + ".png", name + ".wav")
                ).lastrowid
                conn.execute(
                    "INSERT INTO sessions(character_id,chat_id,state) VALUES(?,1,'active')",
                    (ids[name],),
                )
            conn.execute(
                "INSERT INTO sessions(character_id,chat_id,state,pending_data) VALUES(NULL,?,?,?)",
                (1, "awaiting_multichar_scene", json.dumps({
                    "char_a": {"id": ids["Lylah"], "name": "Lylah"},
                    "char_b": {"id": ids["Kaida"], "name": "Kaida"},
                })),
            )
        api.handle_message(
            1, 'Lylah watches Kaida. "I want to join," Carmen remarks.'
        )
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM sessions WHERE character_id IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()[0],
                "awaiting_multichar_scene",
            )
            reply = json.loads(conn.execute(
                "SELECT payload FROM deliveries WHERE chat_id=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])["value"]
            self.assertIn("exactly two characters", reply)
            self.assertIn("Carmen", reply)

    def test_multiimage_flow_enqueues_two_reference_image_without_voices(self):
        with api.get_db() as conn:
            for name in ("Avery", "Blake"):
                character_id = conn.execute(
                    "INSERT INTO characters(name,concept,base_image_path) VALUES(?,?,?)",
                    (name, "adult " + name, name + ".png"),
                ).lastrowid
                conn.execute(
                    "INSERT INTO sessions(character_id,chat_id,state) VALUES(?,1,'active')",
                    (character_id,),
                )
        api.handle_message(1, "/multiimage")
        with api.get_db() as conn:
            picker = api.get_in_progress_session(conn, 1)
            first_list = json.loads(picker["pending_data"])
        api.handle_message(1, "1")
        with api.get_db() as conn:
            picker = api.get_in_progress_session(conn, 1)
            data = json.loads(picker["pending_data"])
            second_index = next(
                index for index, item in enumerate(data["char_list"], start=1)
                if item["id"] != data["char_a"]["id"]
            )
        api.handle_message(1, str(second_index))
        api.handle_message(1, "Both characters walk together on a beach.")
        with api.get_db() as conn:
            job = conn.execute(
                "SELECT job_type,payload FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(job["job_type"], "multi_character_image")
            payload = json.loads(job["payload"])
            self.assertEqual(payload["scene_text"], "Both characters walk together on a beach.")
            self.assertNotIn("voice_sample", payload["char_a"])
            self.assertNotIn("voice_sample", payload["char_b"])

    def test_multiimage_second_picker_does_not_depend_on_active_sessions(self):
        with api.get_db() as conn:
            first = conn.execute(
                "INSERT INTO characters(name,concept,base_image_path) VALUES('Avery','adult','a.png')"
            ).lastrowid
            second = conn.execute(
                "INSERT INTO characters(name,concept,base_image_path) VALUES('Blake','adult','b.png')"
            ).lastrowid
            # Only the first character has an active Telegram session. This was
            # the production trigger for an impossible second-choice loop.
            conn.execute(
                "INSERT INTO sessions(character_id,chat_id,state) VALUES(?,1,'active')",
                (first,),
            )
        api.handle_message(1, "/multiimage")
        with api.get_db() as conn:
            picker = api.get_in_progress_session(conn, 1)
            choices = json.loads(picker["pending_data"])
            first_index = next(i for i, item in enumerate(choices, 1) if item["id"] == first)
        api.handle_message(1, str(first_index))
        with api.get_db() as conn:
            picker = api.get_in_progress_session(conn, 1)
            data = json.loads(picker["pending_data"])
            self.assertEqual(picker["state"], "awaiting_multiimage_b")
            self.assertEqual(data["char_a"]["id"], first)
            self.assertEqual(data["char_list"], [{"id": second, "name": "Blake"}])

    def test_old_impossible_second_picker_recovers_from_all_characters(self):
        with api.get_db() as conn:
            first = conn.execute(
                "INSERT INTO characters(name,concept,base_image_path) VALUES('Avery','adult','a.png')"
            ).lastrowid
            second = conn.execute(
                "INSERT INTO characters(name,concept,base_image_path) VALUES('Blake','adult','b.png')"
            ).lastrowid
            conn.execute(
                "INSERT INTO sessions(character_id,chat_id,state,pending_data) VALUES(NULL,?,?,?)",
                (1, "awaiting_multiimage_b", json.dumps({
                    "char_a": {"id": first, "name": "Avery"},
                    "char_list": [{"id": first, "name": "Avery"}],
                })),
            )
        api.handle_message(1, "1")
        with api.get_db() as conn:
            picker = api.get_in_progress_session(conn, 1)
            data = json.loads(picker["pending_data"])
            self.assertEqual(picker["state"], "awaiting_multiimage_b")
            self.assertEqual(data["char_list"], [{"id": second, "name": "Blake"}])
            reply = json.loads(conn.execute(
                "SELECT payload FROM deliveries ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])["value"]
            self.assertIn("Blake", reply)
            self.assertIn("/cancel", reply)

    def test_multivideo_command_starts_two_character_picker(self):
        with api.get_db() as conn:
            for name in ("Avery", "Blake"):
                character_id = conn.execute(
                    "INSERT INTO characters(name,concept,base_image_path,voice_sample_path) VALUES(?,?,?,?)",
                    (name, "adult " + name, name + ".png", name + ".wav"),
                ).lastrowid
                conn.execute(
                    "INSERT INTO sessions(character_id,chat_id,state) VALUES(?,1,'active')",
                    (character_id,),
                )
        api.handle_message(1, "/multivideo")
        with api.get_db() as conn:
            self.assertEqual(
                api.get_in_progress_session(conn, 1)["state"],
                "awaiting_multichar_a",
            )

    def test_reply_failure_rolls_back_session(self):
        with patch.object(api, "_defer", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                api.handle_message(1, "/newcharacter")
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_snapshot_is_frozen_and_inherited(self):
        with api.get_db() as conn:
            api.set_setting(conn, "content_mode", "sfw")
            api.set_setting(conn, "aspect_mode", "cinematic")
            job = api.enqueue_job(conn, 1, "llm_chat", "light", {"prompt": "hello"})
            snapshot = json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()[0])["settings_snapshot"]
            api.set_setting(conn, "content_mode", "nsfw")
            token = api._job_settings.set(snapshot)
            try:
                child = api.enqueue_job(conn, 1, "character_video", "heavy", {})
            finally:
                api._job_settings.reset(token)
            saved = json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (child,)).fetchone()[0])
            self.assertEqual(saved["settings_snapshot"], snapshot)
            self.assertEqual(snapshot["content_mode"], "sfw")
            self.assertEqual(snapshot["aspect_mode"], "cinematic")

    def test_status_is_chat_scoped_and_failure_not_hidden_by_delivery(self):
        with api.get_db() as conn:
            job = api.enqueue_job(conn, 1, "character_video", "heavy", {})
            api.enqueue_job(conn, 2, "private_other_job", "heavy", {})
            conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job,))
            api._defer(conn, job, "msg", 1, "failed")
            conn.execute("UPDATE deliveries SET status='delivered'")
            text = api.job_status_text(conn, 1)
            self.assertIn("Failed", text)
            self.assertNotIn("private_other_job", text)
            self.assertNotIn("Delivered", text)

    def test_existing_delivery_migration_preserves_rows(self):
        with api.get_db() as conn:
            conn.execute("DROP TABLE deliveries")
            conn.execute("CREATE TABLE deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, chat_id INTEGER, kind TEXT, payload TEXT, status TEXT, next_attempt REAL)")
            conn.execute("INSERT INTO deliveries VALUES (8,3,1,'msg','{}','pending',0)")
            api._migrate_deliveries_nullable(conn)
            api._migrate_deliveries_nullable(conn)
            self.assertEqual(conn.execute("SELECT id,job_id FROM deliveries").fetchone()[:], (8,3))
            conn.execute("INSERT INTO deliveries(job_id) VALUES(NULL)")

    def test_worker_settings_do_not_query_live_api(self):
        import ast
        from pathlib import Path
        tree = ast.parse((Path(api.__file__).parents[1] / "worker/worker.py").read_text(encoding="utf-8"))
        names = {"get_current_model", "get_content_mode", "get_aspect_mode", "get_current_workflow", "get_character_videos_enabled"}
        module = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
        ns = {"_active_settings": {"model":"saved", "content_mode":"sfw", "aspect_mode":"cinematic", "character_videos":False, "workflows":{"image":"saved.json"}}}
        exec(compile(module, "worker_settings", "exec"), ns)
        self.assertEqual(ns["get_current_model"](), "saved")
        self.assertEqual(ns["get_content_mode"](), "sfw")
        self.assertEqual(ns["get_aspect_mode"](), "cinematic")
        self.assertFalse(ns["get_character_videos_enabled"]())
        self.assertEqual(ns["get_current_workflow"]("image"), "saved.json")

    def test_claim_history_includes_new_reply_but_not_future_user(self):
        with api.get_db() as conn:
            character = conn.execute("INSERT INTO characters(name,concept) VALUES('Test','test')").lastrowid
            api.record_message(conn, character, 1, "user", "Earlier question")
            api.record_message(conn, character, 1, "user", "Current question")
            job = api.enqueue_job(conn, 1, "character_chat_intent", "light", {"character_id":character,"message":"Current question"})
            api.record_message(conn, character, 1, "user", "Future question")
            api.record_message(conn, character, 1, "assistant", "Answer to earlier question")
        with api.app.test_client() as client:
            payload = client.get("/jobs/next").get_json()["payload"]
        self.assertEqual([t["content"] for t in payload["conversation_turns"]], ["Earlier question", "Answer to earlier question"])
