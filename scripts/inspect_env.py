"""Portable, read-only status inspection. Never launches or stops services."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime import ROOT, LOG_DIR, STATE_FILE, read_json, load_dotenv


def run():
    load_dotenv()
    print("Project:", ROOT)
    state = read_json(STATE_FILE)
    print("Supervisor state:", json.dumps(state))
    urls = {"VPS": os.environ.get("VPS_URL", "http://127.0.0.1:" + os.environ.get("API_PORT", "8787")) + "/health",
            "ComfyUI": os.environ.get("COMFY_URL", "http://127.0.0.1:8188") + "/system_stats",
            "Ollama": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/version"}
    for name, url in urls.items():
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                print(name, "HTTP", response.status)
        except Exception as exc:
            print(name, type(exc).__name__)
    for name in ("supervisor", "vps", "worker"):
        beat = read_json(LOG_DIR / (name + ".heartbeat"))
        age = round(time.time() - beat["at"], 1) if beat.get("at") else None
        print(name, "heartbeat age seconds:", age, "pid:", beat.get("pid"))
    db = Path(os.environ.get("DB_PATH", ROOT / "vps/jobs.db"))
    if db.exists():
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            print("Jobs:", conn.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='deliveries'").fetchone():
                print("Deliveries:", conn.execute("SELECT status,count(*) FROM deliveries GROUP BY status").fetchall())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        run()
        if not args.watch:
            break
        time.sleep(5)
