"""Read-only report: identifies finished videos; never sends or changes jobs."""
import json
import sqlite3
from pathlib import Path
import requests


def completed_video(entry):
    status = entry.get("status", {})
    if status.get("status_str") != "success" or not status.get("completed"):
        return None
    files = [f for out in entry.get("outputs", {}).values()
             for key in ("videos", "gifs", "images") for f in out.get(key, [])
             if Path(f.get("filename", "")).suffix.lower() == ".mp4" and f.get("type") == "output"]
    return files[0] if len(files) == 1 else None


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.runtime import load_dotenv
    from scripts.supervisor import resolve_db_path
    import os
    load_dotenv()
    db = resolve_db_path(os.environ.get("DB_PATH"))
    with sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT id,progress_json FROM jobs WHERE status IN ('interrupted','dispatched') AND job_type IN ('character_video','multi_character_video')").fetchall()
    print(f"Unresolved video jobs: {len(rows)}")
    for job_id, progress in rows:
        try:
            prompts = json.loads(progress or "{}").get("comfy_prompts", [])
            if not prompts or not isinstance(prompts[-1], dict):
                print(job_id, "no recorded prompt")
                continue
            record = prompts[-1]
            response = requests.get(record["endpoint"].rstrip("/") + "/history/" + record["prompt_id"], timeout=10)
            response.raise_for_status()
            file = completed_video(response.json().get(record["prompt_id"], {}))
            print(job_id, "completed video available" if file else "no unambiguous completed video")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(job_id, "inspection unavailable", type(exc).__name__)


if __name__ == "__main__":
    main()
