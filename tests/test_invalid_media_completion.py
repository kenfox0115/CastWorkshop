"""Chat media plans are repaired without failing or duplicating the reply."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import requests
from tests.test_jobs import api, test_delivery
from worker.job_runtime import JobClient


class InvalidMediaCompletionTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    tearDown = test_delivery.DeliveryTests.tearDown

    def make_job(self, transport):
        with api.get_db() as conn:
            context = api.ensure_transport_context(conn, transport, 1)
            character = conn.execute("INSERT INTO characters(name,concept,base_image_path) VALUES('Test','adult character','ref.png')").lastrowid
            payload = json.dumps({"character_id": character, "message": "send a photo"})
            job = conn.execute("INSERT INTO jobs(chat_id,context_id,job_type,weight,payload,status,lease_token) VALUES(1,?,'character_chat_intent','light',?,'dispatched','owner')", (context, payload)).lastrowid
            queued = conn.execute("INSERT INTO jobs(chat_id,job_type,weight,payload,status) VALUES(2,'test','light','{}','queued')").lastrowid
        return job, queued

    def test_conflicting_plans_are_repaired_and_idempotent(self):
        for transport, kind in (("telegram", "image"), ("web", "video")):
            with self.subTest(transport=transport, kind=kind):
                job, queued = self.make_job(transport)
                body = {"ok": True, "lease_token": "owner", "text": "Here you go.",
                        "response_type": kind, "scene": "Fully nude and wearing a dress."}
                with api.app.test_client() as client:
                    self.assertEqual(client.post(f"/jobs/{job}/complete", json=body).status_code, 200)
                    self.assertEqual(client.post(f"/jobs/{job}/complete", json=body).get_json()["status"], "ignored")
                    self.assertIsNotNone(client.get("/jobs/next").get_json())
                with api.get_db() as conn:
                    row = conn.execute("SELECT status,result FROM jobs WHERE id=?", (job,)).fetchone()
                    self.assertEqual(row["status"], "done")
                    self.assertEqual(conn.execute("SELECT count(*) FROM messages WHERE content='Here you go.'").fetchone()[0], 1 if job == 1 else 2)
                    followup = conn.execute("SELECT payload FROM jobs WHERE job_type=? ORDER BY id DESC LIMIT 1", ("character_image_edit" if kind == "image" else "character_video",)).fetchone()
                    prompt = json.loads(followup[0]).get("prompt") or json.loads(followup[0])["scene_prompt"]
                    self.assertIn("fully nude", prompt)
                    self.assertNotIn("wearing a dress", prompt)
                    self.assertEqual(conn.execute("SELECT count(*) FROM deliveries WHERE job_id=?", (job,)).fetchone()[0], 0)

    def test_saved_completion_drains_through_real_route(self):
        job, queued = self.make_job("telegram")
        class Session:
            def post(self, url, json, timeout):
                with api.app.test_client() as client:
                    response = client.post(url.split("http://test")[1], json=json)
                result = requests.Response()
                result.status_code = response.status_code
                result._content = response.data
                return result
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{job}.owner.pending.json"
            path.write_text(json.dumps({"url": f"http://test/jobs/{job}/complete", "body": {
                "ok": True, "lease_token": "owner", "text": "Here you go.",
                "response_type": "image", "scene": "Fully nude and wearing a dress."}}))
            self.assertTrue(JobClient("http://test", directory, Session()).drain())
            self.assertFalse(path.exists())
        with api.app.test_client() as client:
            self.assertIsNotNone(client.get("/jobs/next").get_json())

    def test_unrelated_storage_failure_still_rolls_back(self):
        job, _ = self.make_job("telegram")
        with api.app.test_client() as client, patch.object(api, "record_message", side_effect=RuntimeError("disk error")):
            with self.assertRaisesRegex(RuntimeError, "disk error"):
                client.post(f"/jobs/{job}/complete", json={"ok": True, "lease_token": "owner", "text": "Hello", "response_type": "chat"})
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0], "dispatched")

    def test_failed_chat_attempt_retries_without_user_error(self):
        job, _ = self.make_job("telegram")
        with api.app.test_client() as client:
            body = {"ok": False, "lease_token": "owner", "text": "ValueError: bad plan"}
            response = client.post(f"/jobs/{job}/complete", json=body).get_json()
            self.assertEqual(response["status"], "ok")
            self.assertEqual(client.post(f"/jobs/{job}/complete", json=body).get_json()["status"], "ignored")
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE status='failed'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            retry = conn.execute("SELECT payload,status FROM jobs WHERE id=?", (response["followup_job_id"],)).fetchone()
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(json.loads(retry["payload"])["internal_retry_count"], 1)
        with api.app.test_client() as client:
            self.assertIsNotNone(client.get("/jobs/next").get_json())
            self.assertIsNone(client.get("/jobs/next").get_json())
        with api.get_db() as conn:
            payload = json.loads(retry["payload"])
            payload["internal_retry_after"] = 0
            conn.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), response["followup_job_id"]))
        with api.app.test_client() as client:
            self.assertEqual(client.get("/jobs/next").get_json()["id"], response["followup_job_id"])

    def test_unexpected_validation_error_queues_planning_only_repair(self):
        job, _ = self.make_job("telegram")
        body = {"ok": True, "lease_token": "owner", "text": "Here you go.",
                "response_type": "image", "scene": "Sitting at a cafe."}
        with api.app.test_client() as client, patch.object(api, "compose_chat_media_scene", side_effect=ValueError("invalid authored plan")):
            first = client.post(f"/jobs/{job}/complete", json=body).get_json()
        with api.get_db() as conn:
            retry = conn.execute("SELECT * FROM jobs WHERE id=?", (first["followup_job_id"],)).fetchone()
            payload = json.loads(retry["payload"])
            self.assertEqual(payload["drafted_reply"], "Here you go.")
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
            conn.execute("UPDATE jobs SET status='dispatched',lease_token='repair-owner' WHERE id=?", (retry["id"],))
        body["lease_token"] = "repair-owner"
        with api.app.test_client() as client:
            self.assertEqual(client.post(f"/jobs/{retry['id']}/complete", json=body).status_code, 200)
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
