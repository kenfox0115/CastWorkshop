"""Lease/recovery API and durable worker completion regression tests."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_delivery
api = test_delivery.api
from worker.job_runtime import JobClient, CompletionPending

class LeaseTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    tearDown = test_delivery.DeliveryTests.tearDown

    def enqueue(self, weight):
        with api.get_db() as conn:
            return conn.execute("INSERT INTO jobs(chat_id,job_type,weight,payload) VALUES(1,'test',?,'{}')", (weight,)).lastrowid

    def claim(self):
        with api.app.test_client() as c:
            return c.get("/jobs/next?worker_id=test-worker").get_json()

    def test_renewed_long_job_is_not_requeued(self):
        job_id = self.enqueue("heavy")
        job = self.claim()
        with api.get_db() as conn:
            conn.execute("UPDATE jobs SET dispatched_at='2000-01-01',lease_expires_at=0")
        with api.app.test_client() as c:
            response = c.post(f"/jobs/{job_id}/lease", json={"lease_token": job["lease_token"], "progress": {"comfy_prompts": ["render-1"]}})
            self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.claim())
        with api.get_db() as conn:
            self.assertIn("render-1", conn.execute("SELECT progress_json FROM jobs").fetchone()[0])

    def test_expired_media_is_held_and_late_owner_can_finish(self):
        job_id = self.enqueue("heavy")
        job = self.claim()
        with api.get_db() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at=0")
        self.assertIsNone(self.claim())
        with api.app.test_client() as c:
            self.assertEqual(c.get("/jobs/recovery").get_json()[0]["status"], "interrupted")
            self.assertEqual(c.post(f"/jobs/{job_id}/complete", json={"lease_token": job["lease_token"], "ok":True,"text":"finished"}).status_code, 200)

    def test_inspected_interrupted_job_can_be_requeued_with_new_owner(self):
        job_id=self.enqueue("heavy");old=self.claim()
        with api.get_db() as conn:conn.execute("UPDATE jobs SET lease_expires_at=0")
        with api.app.test_client() as c:
            interrupted=c.get("/jobs/recovery").get_json()[0]
            recovered=c.post(f"/jobs/{job_id}/recover",json={"lease_token":interrupted["lease_token"]})
            self.assertEqual(recovered.status_code,200)
        new=self.claim()
        self.assertEqual(new["id"],job_id);self.assertNotEqual(new["lease_token"],old["lease_token"])
        with api.app.test_client() as c:
            self.assertEqual(c.post(f"/jobs/{job_id}/complete",json={"lease_token":old["lease_token"],"ok":True}).status_code,409)

    def test_expired_light_job_rejects_previous_owner(self):
        job_id = self.enqueue("light")
        old = self.claim()
        with api.get_db() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at=0")
        new = self.claim()
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with api.app.test_client() as c:
            self.assertEqual(c.post(f"/jobs/{job_id}/complete", json={"lease_token":old["lease_token"],"ok":True,"text":"stale"}).status_code, 409)
            self.assertEqual(c.post(f"/jobs/{job_id}/lease", json={"lease_token":old["lease_token"]}).status_code, 409)
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

class SpoolTests(unittest.TestCase):
    def test_progress_metadata_is_spooled_and_sent_with_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Mock()
            session.post.return_value.status_code = 200
            client = JobClient("http://test", directory, session)
            client.job = {"id": 9, "lease_token": "owner"}
            client.progress = {"comfy_prompts": []}
            client.update_progress(prompt_template="v2", video_seed=123, duration_seconds=6.5)
            sent = session.post.call_args.kwargs["json"]["progress"]
            self.assertEqual(sent["prompt_template"], "v2")
            self.assertEqual(sent["video_seed"], 123)
            saved = json.loads((Path(directory) / "9.progress.json").read_text())
            self.assertEqual(saved["duration_seconds"], 6.5)

    def test_http_500_retains_success_and_restart_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Mock()
            response = Mock(status_code=500)
            response.raise_for_status.side_effect = requests.HTTPError("500")
            session.post.return_value = response
            client = JobClient("http://test", directory, session)
            client.job = {"id": 7, "lease_token": "owner"}
            with self.assertRaises(CompletionPending):
                client.post("http://test/jobs/7/complete", json={"ok":True,"video_b64":"media"})
            path = next(Path(directory).glob("*.pending.json"))
            self.assertTrue(json.loads(path.read_text())["body"]["ok"])
            with self.assertRaises(CompletionPending):
                client.post("http://test/jobs/7/complete", json={"ok":False,"text":"transport error"})
            self.assertTrue(path.exists())
            response = Mock(status_code=200)
            response.json.return_value = {"status":"ok"}
            session.post.return_value = response
            restarted = JobClient("http://test", directory, session)
            self.assertTrue(restarted.drain())
            self.assertFalse(path.exists())

    def test_conflicting_completion_kept_for_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Mock()
            session.post.return_value.raise_for_status.side_effect = requests.HTTPError("409")
            client = JobClient("http://test", directory, session)
            client.job = {"id":8,"lease_token":"old"}
            with self.assertRaises(CompletionPending):
                client.post("http://test/jobs/8/complete", json={"ok":True,"text":"retained"})
            self.assertFalse(client.drain())
            self.assertEqual(len(list(Path(directory).glob("*.pending.json"))), 1)

if __name__ == "__main__":
    unittest.main()
