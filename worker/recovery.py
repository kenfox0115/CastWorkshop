"""Safely recover interrupted ComfyUI jobs after a worker or machine restart."""
import base64
import json
import logging
import requests
from scripts.runtime import atomic_json
from scripts.recovery_report import completed_video


def _contains(value,needle):
    if value==needle:return True
    if isinstance(value,dict):return any(_contains(item,needle) for item in value.values())
    if isinstance(value,list):return any(_contains(item,needle) for item in value)
    return False


def recover_videos(client, endpoint, session=requests):
    """Compatibility name; recovers videos and safely requeues all other interrupted work."""
    response=session.get(client.base_url+"/jobs/recovery",timeout=10);response.raise_for_status()
    jobs=response.json()
    if not jobs:return
    queue_response=session.get(endpoint.rstrip("/")+"/queue",timeout=10);queue_response.raise_for_status()
    queue=queue_response.json()
    for job in jobs:
        try:
            progress=json.loads(job.get("progress_json") or "{}")
            # A submission can have reached ComfyUI before its response was
            # lost. Earlier pass IDs cannot identify this newest request.
            if progress.get("phase") == "submitting":
                logging.warning("Interrupted job %s has an ambiguous ComfyUI submission; manual history inspection required",job["id"])
                continue
            prompts=progress.get("comfy_prompts",[])
            record=prompts[-1] if prompts and isinstance(prompts[-1],dict) else None
            if record and record.get("endpoint","").rstrip("/")!=endpoint.rstrip("/"):
                continue
            prompt_id=record.get("prompt_id") if record else None
            if not prompt_id and (queue.get("queue_running") or queue.get("queue_pending")):
                logging.warning("Interrupted job %s has no prompt ID while ComfyUI is busy; recovery deferred",job["id"])
                continue
            if prompt_id and _contains(queue,prompt_id):
                logging.info("Interrupted job %s is still present in ComfyUI queue",job["id"])
                continue
            history_entry={}
            if prompt_id:
                history=session.get(endpoint.rstrip("/")+"/history/"+prompt_id,timeout=10)
                history.raise_for_status()
                history_entry=history.json().get(prompt_id,{})
            if job["job_type"] in ("character_video","multi_character_video") and history_entry:
                file=completed_video(history_entry)
                if file is not None:
                    video=session.get(endpoint.rstrip("/")+"/view",params=file,timeout=30)
                    video.raise_for_status()
                    if video.content:
                        token=job["lease_token"];path=client.spool/f"{job['id']}.{token}.pending.json"
                        if not path.exists():
                            atomic_json(path,{"url":f"{client.base_url}/jobs/{job['id']}/complete",
                              "body":{"ok":True,"lease_token":token,
                                      "video_b64":base64.b64encode(video.content).decode("ascii")}})
                        logging.info("Recovered completed video for job %s",job["id"])
                        continue
            recovered=session.post(client.base_url+f"/jobs/{job['id']}/recover",
              json={"lease_token":job.get("lease_token")},timeout=10)
            if recovered.status_code not in (200,409):recovered.raise_for_status()
            if recovered.ok:logging.info("Requeued interrupted job %s after ComfyUI inspection",job["id"])
        except (requests.RequestException,ValueError,KeyError,TypeError,OSError) as exc:
            logging.warning("Recovery deferred for job %s (%s)",job.get("id"),type(exc).__name__)
