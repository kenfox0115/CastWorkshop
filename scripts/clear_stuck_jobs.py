"""Inspect unresolved jobs. This command never marks work done or vacuums the DB."""
from pathlib import Path
import sqlite3

def main():
    path = Path(__file__).resolve().parents[1] / "vps" / "jobs.db"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT id,job_type,status,dispatched_at FROM jobs WHERE status NOT IN ('done','failed') ORDER BY id").fetchall()
    for row in rows:
        print(row)
    print(f"{len(rows)} unresolved jobs. Inspect ComfyUI history before deciding recovery; no changes made.")

if __name__ == "__main__":
    main()
