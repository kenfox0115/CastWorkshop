"""Renew job ownership and persist results before attempting completion HTTP."""
import json
import logging
from pathlib import Path
import threading
import time
import uuid
import requests
from scripts.runtime import atomic_json


class CompletionPending(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class JobClient:
    def __init__(self, base_url, spool, session=requests):
        self.base_url = base_url.rstrip("/")
        self.spool = Path(spool)
        self.worker_id = uuid.uuid4().hex
        self.session = session
        self.job = None
        self.progress = {}
        self.guard = threading.Lock()
        self.renew_guard = threading.Lock()
        self.stop_event = threading.Event()
        self.lost = False
        self.thread = None

    def begin(self, job):
        self.job = job
        self.progress = {"comfy_prompts": []}
        self.lost = False
        self.stop_event.clear()
        self.renew()
        self.thread = threading.Thread(target=self._renew_loop, daemon=True, name="job-lease")
        self.thread.start()

    def _renew_loop(self):
        while not self.stop_event.wait(20):
            try:
                self.renew()
            except Exception as exc:
                logging.warning("Job lease renewal failed (%s)", type(exc).__name__)

    def renew(self):
        with self.renew_guard:
            self._renew()

    def _renew(self):
        with self.guard:
            progress = dict(self.progress)
        response = self.session.post(f"{self.base_url}/jobs/{self.job['id']}/lease",
            json={"lease_token": self.job["lease_token"], "progress": progress}, timeout=10)
        if response.status_code == 409:
            self.lost = True
            raise LeaseLost("Job lease no longer owned")
        response.raise_for_status()

    def record_prompt(self, phase, prompt_id=None, endpoint=None):
        if self.lost:
            raise LeaseLost("Refusing another render after lease loss")
        with self.guard:
            self.progress["phase"] = phase
            if prompt_id:
                self.progress["comfy_prompts"].append({"prompt_id": prompt_id, "endpoint": endpoint})
            value = {"job_id": self.job["id"], "lease_token": self.job["lease_token"], **self.progress}
            atomic_json(self.spool / f"{self.job['id']}.progress.json", value)
        self.renew()  # require ownership before another external submission

    def update_progress(self, **fields):
        """Durably attach diagnostic metadata without bypassing VPS ownership."""
        if self.lost:
            raise LeaseLost("Refusing progress update after lease loss")
        with self.guard:
            self.progress.update(fields)
            value = {"job_id": self.job["id"], "lease_token": self.job["lease_token"], **self.progress}
            atomic_json(self.spool / f"{self.job['id']}.progress.json", value)
        self.renew()

    def end(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)
        self.thread = None
        self.job = None

    def _send_saved(self, path):
        saved = json.loads(path.read_text(encoding="utf-8"))
        response = self.session.post(saved["url"], json=saved["body"], timeout=30)
        response.raise_for_status()
        if response.json().get("status") not in ("ok", "ignored"):
            raise CompletionPending("Unexpected completion acknowledgment")
        path.unlink()
        return response

    def post(self, url, json, timeout=30):
        body = dict(json)
        body["lease_token"] = self.job["lease_token"]
        path = self.spool / f"{self.job['id']}.{self.job['lease_token']}.pending.json"
        # Never overwrite an already-generated success with a transport failure.
        if path.exists():
            saved = __import__("json").loads(path.read_text(encoding="utf-8"))
            if saved["body"].get("ok") and not body.get("ok"):
                raise CompletionPending("Successful result already retained for retry")
        atomic_json(path, {"url": url, "body": body})
        try:
            return self._send_saved(path)
        except Exception as exc:
            raise CompletionPending(f"Result retained for retry ({type(exc).__name__})") from exc

    def drain(self):
        for path in sorted(self.spool.glob("*.pending.json")):
            try:
                self._send_saved(path)
            except Exception as exc:
                logging.warning("Completion %s retained (%s); no new job claimed", path.name, type(exc).__name__)
                return False
        return True
