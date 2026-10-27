"""Resolve a failed operation to the beginning of its candidate selection step."""
import json

STEP_JOBS = {
    "voice_sample_batch": ("voice_sample_batch", "voice_generating", "voice"),
    "promote_voice": ("voice_sample_batch", "voice_generating", "voice"),
    "character_image_batch": ("character_image_batch", "image_queued", "look"),
    "promote_reference": ("character_image_batch", "image_queued", "look"),
}


def restart_step(conn, row):
    spec = STEP_JOBS.get(row["job_type"])
    if not spec:
        raise ValueError("This job has no selection step to restart.")
    job_type, state, label = spec
    character_id = json.loads(row["payload"]).get("character_id")
    character = conn.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    if not character:
        raise ValueError("The character used by this step no longer exists.")
    if label == "voice" and not character["base_image_path"]:
        raise ValueError("Choose a character appearance before restarting voice selection.")
    source = conn.execute(
        """SELECT payload FROM jobs WHERE context_id IS ? AND chat_id=? AND job_type=?
           AND json_extract(payload,'$.character_id')=? AND id<=? ORDER BY id DESC LIMIT 1""",
        (row["context_id"], row["chat_id"], job_type, character_id, row["id"]),
    ).fetchone()
    if not source:
        raise ValueError("The original candidate generation request is unavailable.")
    payload = json.loads(source["payload"])
    for key in ("settings_snapshot", "user_message_id", "retry_of_job_id"):
        payload.pop(key, None)
    payload["retry_of_job_id"] = row["id"]
    payload["restart_step"] = label
    return job_type, state, payload
