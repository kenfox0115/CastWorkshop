"""Real Flask routes + temporary SQLite; fake Telegram only."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_IMPORT_LOGS = tempfile.TemporaryDirectory()
os.environ["CC_LOG_DIR"] = _IMPORT_LOGS.name
os.environ["TELEGRAM_ENABLED"] = "0"
from vps import app as api
from vps import artifacts
import atexit
atexit.register(api._rot.close)
from vps.delivery import DeliveryWorker

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        api.DB_PATH = str(Path(self.temp.name) / "jobs.db")
        api.init_db()
        api.app.config["TESTING"] = True

    def tearDown(self):
        self.temp.cleanup()

    def job(self, chat=1):
        with api.get_db() as conn:
            return conn.execute("INSERT INTO jobs(chat_id,job_type,payload,status) VALUES(?, 'test', '{}', 'dispatched')", (chat,)).lastrowid

    def complete(self, job):
        with api.app.test_client() as client:
            return client.post(f"/jobs/{job}/complete", json={"ok": True, "video_b64": base64.b64encode(b"video data").decode()})

    def test_ack_does_not_call_telegram_and_is_idempotent(self):
        job = self.job()
        with api.get_db() as conn:
            context=api.ensure_transport_context(conn,"telegram",1)
            conn.execute("UPDATE jobs SET context_id=? WHERE id=?",(context,job))
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)),patch.object(api, "send_telegram_video", side_effect=AssertionError("network in request")):
            self.assertEqual(self.complete(job).status_code, 200)
            self.assertEqual(self.complete(job).status_code, 200)
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0], "done")
            delivery=json.loads(conn.execute("SELECT payload FROM deliveries").fetchone()[0])
            button=delivery["kwargs"]["reply_markup"]["inline_keyboard"][0][0]
            self.assertEqual(button["text"],"Continue video")
            self.assertTrue(button["callback_data"].startswith("continue:"))

    def test_concurrent_completions_keep_all_deliveries(self):
        jobs = [self.job(i) for i in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertTrue(all(x.status_code == 200 for x in pool.map(self.complete, jobs)))
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 12)

    def test_transaction_rollback_does_not_mark_job_done(self):
        job = self.job()
        with patch.object(api, "_defer", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                self.complete(job)
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0], "dispatched")
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_delivery_retries_after_restart_and_keeps_media(self):
        self.complete(self.job())
        worker = DeliveryWorker(api.get_db, api.db_lock, {"video": lambda *a, **kw: False})
        self.assertTrue(worker.run_once())
        with api.get_db() as conn:
            row = conn.execute("SELECT * FROM deliveries").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertIn("dmlkZW8gZGF0YQ==", row["payload"])
            conn.execute("UPDATE deliveries SET next_attempt=0")
        delivered = []
        worker = DeliveryWorker(api.get_db, api.db_lock, {"video": lambda chat, data, **kw: delivered.append(data) or True})
        self.assertTrue(worker.run_once())
        self.assertEqual(delivered, [b"video data"])
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM deliveries").fetchone()[0], "delivered")

    def test_failed_delivery_can_be_retried_explicitly(self):
        self.complete(self.job())
        with api.get_db() as conn:
            conn.execute("UPDATE deliveries SET status='failed',attempts=8")
        with api.app.test_client() as c:
            self.assertEqual(c.post("/jobs/deliveries/1/retry").status_code, 200)
        with api.get_db() as conn:
            row = conn.execute("SELECT status,attempts,payload FROM deliveries").fetchone()
            self.assertEqual(tuple(row)[:2], ("pending", 0))
            self.assertNotEqual(row["payload"], "{}")

    def test_legacy_database_is_migrated_without_losing_jobs(self):
        with api.get_db() as conn:
            conn.execute("DROP TABLE jobs")
            conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,chat_id INTEGER,job_type TEXT,weight TEXT,payload TEXT,status TEXT,result TEXT,created_at TEXT,dispatched_at TEXT,completed_at TEXT,priority INTEGER)")
            conn.execute("INSERT INTO jobs(id,chat_id,status) VALUES(99,1,'done')")
        api.init_db()
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=99").fetchone()[0], "done")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            self.assertTrue({"lease_token","lease_expires_at","worker_id","progress_json"}.issubset(columns))

    def test_abandoned_delivery_is_recovered(self):
        self.complete(self.job())
        with api.get_db() as conn:
            conn.execute("UPDATE deliveries SET status='sending',lease_until=0")
        worker = DeliveryWorker(api.get_db, api.db_lock, {"video": lambda *a, **kw: True})
        self.assertTrue(worker.run_once())

if __name__ == "__main__":
    unittest.main()
