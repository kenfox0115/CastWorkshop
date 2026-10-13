"""Transactional delivery records and a single retrying dispatcher.

Generation acceptance and Telegram delivery are separate durable states.
Never hold the application DB lock during a network upload.
"""
import base64
import json
import logging
import threading
import time




def enqueue(conn, job_id, kind, chat_id, value, **kwargs):
    payload = {"value": value if isinstance(value, str) else base64.b64encode(value).decode("ascii"),
               "binary": not isinstance(value, str), "kwargs": kwargs}
    conn.execute("INSERT INTO deliveries(job_id,chat_id,kind,payload) VALUES(?,?,?,?)",
                 (job_id, chat_id, kind, json.dumps(payload)))


class DeliveryWorker:
    def __init__(self, db_factory, db_lock, senders):
        self.db_factory, self.db_lock, self.senders = db_factory, db_lock, senders
        self.stop_event = threading.Event()

    def run_once(self):
        now = time.time()
        with self.db_lock, self.db_factory() as conn:
            conn.execute("UPDATE deliveries SET status='pending' WHERE status='sending' AND lease_until < ?", (now,))
            row = conn.execute("""SELECT d.* FROM deliveries d
                WHERE d.status='pending' AND d.next_attempt <= ?
                  AND NOT EXISTS (SELECT 1 FROM deliveries earlier
                    WHERE earlier.chat_id=d.chat_id AND earlier.id<d.id
                      AND earlier.status IN ('pending','sending'))
                ORDER BY d.id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE deliveries SET status='sending', attempts=attempts+1, lease_until=? WHERE id=?",
                         (now + 600, row["id"]))
        try:
            body = json.loads(row["payload"])
            value = base64.b64decode(body["value"]) if body["binary"] else body["value"]
            ok = self.senders[row["kind"]](row["chat_id"], value, **body["kwargs"])
            if not ok:
                raise RuntimeError("Telegram did not confirm delivery")
            with self.db_lock, self.db_factory() as conn:
                conn.execute("UPDATE deliveries SET status='delivered',payload='{}',delivered_at=?,lease_until=NULL,last_error=NULL WHERE id=?",
                             (time.time(), row["id"]))
        except Exception as exc:
            attempts = row["attempts"] + 1
            # Avoid retaining exception URLs: they can contain the Telegram credential.
            error = type(exc).__name__
            with self.db_lock, self.db_factory() as conn:
                conn.execute("UPDATE deliveries SET status=?,next_attempt=?,lease_until=NULL,last_error=? WHERE id=?",
                    ("failed" if attempts >= 8 else "pending", time.time() + min(300, 2 ** attempts), error, row["id"]))
            logging.warning("Delivery %s attempt %s failed (%s); payload retained", row["id"], attempts, error)
        return True

    def run(self):
        while not self.stop_event.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                logging.exception("Delivery dispatcher error; pending records retained")
            self.stop_event.wait(1)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="telegram-delivery")
        self.thread.start()
        return self
