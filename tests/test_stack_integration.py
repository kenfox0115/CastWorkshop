"""Real supervisor/API/worker on temporary ports, with a fake local Ollama.

No Telegram calls, real model requests, or production database changes.
"""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from contextlib import closing
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

class FakeEngine(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.reply({"queue_running":[],"queue_pending":[],"models":[]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/api/chat":
            time.sleep(31)  # actual worker remains inside a long model request
        self.reply({"message":{"content":"Synthetic integration reply."},"done":True})

    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

class StackIntegrationTests(unittest.TestCase):
    def test_real_stack_survives_old_heartbeat_limit_and_stops_cleanly(self):
        self.exercise_stack("127.0.0.1")

    def test_explicit_lan_stack_binds_all_interfaces_and_stops_cleanly(self):
        self.exercise_stack("0.0.0.0")

    def exercise_stack(self, api_host):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
            thread = threading.Thread(target=fake.serve_forever, daemon=True)
            thread.start()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            engine_url = f"http://127.0.0.1:{fake.server_port}"
            base = f"http://127.0.0.1:{port}"
            db = folder / "jobs.db"
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript((ROOT / "vps/schema.sql").read_text(encoding="utf-8"))
                conn.executemany("INSERT INTO settings(key,value) VALUES(?,?)", [("endpoint_ollama",engine_url),("endpoint_comfyui",engine_url),("current_model","fake-model")])
                conn.execute("INSERT INTO jobs(chat_id,job_type,payload) VALUES(1,'llm_chat',?)", (json.dumps({"prompt":"Integration test"}),))
            env = dict(os.environ)
            env.update(CC_LOCK_DIR=str(folder / "locks"), CC_LOG_DIR=str(folder), TELEGRAM_ENABLED="0", TELEGRAM_BOT_TOKEN="",
                WORKER_ENABLED="1", API_PORT=str(port), API_HOST=api_host, VPS_URL=base,
                COMFY_URL=engine_url, OLLAMA_URL=engine_url, DB_PATH=str(db),
                PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", POLL_SECONDS="0.1", PEAK_HOURS_GATE="0")
            for key in ("PYTHONPATH", "PYTHONHOME"):
                env.pop(key, None)
            with open(folder / "test-supervisor.log", "w", encoding="utf-8") as log:
                proc = subprocess.Popen([sys.executable,"-B",str(ROOT / "scripts/supervisor.py")], cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 20
                    state = {}
                    while time.monotonic() < deadline:
                        self.assertIsNone(proc.poll(), "supervisor exited during startup")
                        try:
                            state = json.loads((folder / ".stack_pids.json").read_text())
                            if len(state.get("children", {})) == 2:
                                break
                        except (OSError, ValueError):
                            pass
                        time.sleep(0.2)
                    self.assertEqual(len(state.get("children", {})), 2)
                    with urllib.request.urlopen(base + "/health", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    self.assertIn(f"serving HTTP on {api_host}:{port}",
                                  (folder / "vps.log").read_text(encoding="utf-8"))
                    # Verify the actual HTTP listener: a second address works only
                    # in explicitly configured LAN mode.
                    with closing(HTTPConnection("127.0.0.2", port, timeout=2)) as client:
                        if api_host == "0.0.0.0":
                            client.request("GET", "/health")
                            response = client.getresponse()
                            self.assertEqual(response.status, 200)
                            response.read()
                        else:
                            with self.assertRaises((ConnectionRefusedError, TimeoutError)):
                                client.request("GET", "/health")
                    duplicate = subprocess.run([sys.executable,"-B",str(ROOT / "start.py")],cwd=ROOT,env=env,capture_output=True,text=True,timeout=10)
                    self.assertNotEqual(duplicate.returncode, 0)
                    # Cross the old 30-second false-deadlock threshold with real processes.
                    time.sleep(35)
                    after = json.loads((folder / ".stack_pids.json").read_text())
                    self.assertEqual(state["children"], after["children"])
                    with closing(sqlite3.connect(db)) as conn, conn:
                        row = conn.execute("SELECT status,result FROM jobs WHERE id=1").fetchone()
                        self.assertEqual(row[0], "done")
                        self.assertIn("Synthetic integration reply", row[1])
                        self.assertEqual(conn.execute("SELECT status FROM deliveries").fetchone()[0], "pending")
                    print("Integration: HTTP 200; real worker result committed; delivery pending with Telegram disabled; PIDs stable through a >30s model request.")
                finally:
                    stopped = subprocess.run([sys.executable,"-B",str(ROOT / "stop.py")],cwd=ROOT,env=env,capture_output=True,text=True,timeout=40)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        proc.wait(timeout=5)
                    fake.shutdown()
                    fake.server_close()
                    thread.join(timeout=2)
                self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
                self.assertEqual(proc.returncode, 0, (folder / "supervisor.log").read_text())
                self.assertFalse((folder / ".stack_pids.json").exists())
                print("Integration: scoped stop acknowledged; supervisor and its children stopped.")

if __name__ == "__main__":
    unittest.main()
