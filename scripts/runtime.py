"""Process ownership and liveness primitives; no process-name sweeps."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = Path(os.environ.get("CC_LOG_DIR", ROOT / "logs"))
STATE_FILE = LOG_DIR / ".stack_pids.json"
STOP_FILE = LOG_DIR / ".stop-request.json"


def project_python(root=ROOT):
    """Use the interpreter populated by setup when this copy has a virtualenv."""
    venv = Path(root) / ".venv"
    if not venv.exists():
        return sys.executable
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        raise FileNotFoundError(
            f"Project environment is incomplete: {interpreter}. "
            "Rerun python setup_project.py."
        )
    return str(interpreter)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(value), encoding="utf-8")
        # Windows readers/antivirus can briefly prevent replacing an open file.
        # Preserve atomic publication and retry only Windows lock/access errors.
        for attempt in range(9):
            try:
                os.replace(temp, path)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 8:
                    raise
                time.sleep(min(0.025 * 2 ** attempt, 0.25))
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class InstanceLock:
    """Nonblocking file lock with a small stdlib adapter for Windows/POSIX.

    File contents are not ownership: the OS releases the lock after a crash.
    Keep lock files in place to avoid inode replacement races on POSIX.
    CC_LOCK_DIR can select a shared lock directory for service deployments.
    """
    def __init__(self, identity):
        self.key = hashlib.sha256(identity.encode()).hexdigest()
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return True
        import tempfile
        directory = Path(os.environ.get("CC_LOCK_DIR", str(Path(tempfile.gettempdir()) / "charactercreator-locks")))
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / (self.key + ".lock"), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Another instance already owns this resource")
        return self

    def __exit__(self, *args):
        self.close()


def stack_identity():
    return "stack:" + os.path.normcase(str(ROOT))


def resource_identity(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    host = (u.hostname or "localhost").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        host = "loopback"
    return f"gpu:{host}:{u.port or (443 if u.scheme == "https" else 80)}"


class Heartbeat:
    """Liveness thread independent of long polls/renders; diagnostic, not a kill signal."""
    def __init__(self, name, interval=5):
        self.path = LOG_DIR / (name + ".heartbeat")
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.last_tick = time.monotonic()
        self.fault_log = None

    def tick(self):
        now = time.monotonic()
        if now - self.last_tick > max(60, self.interval * 3):
            import logging
            logging.warning("Heartbeat gap %.1fs in %s pid=%s", now - self.last_tick, self.path.stem, os.getpid())
        self.last_tick = now
        atomic_json(self.path, {"pid": os.getpid(), "parent_pid": os.getppid(),
                                "at": time.time()})

    def start(self):
        import faulthandler
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.fault_log = open(self.path.with_suffix(".fault.log"), "a", encoding="utf-8")
        faulthandler.enable(file=self.fault_log, all_threads=True)
        self.tick()
        self.thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.tick()
            except OSError:
                pass

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=self.interval + 1)
        if self.fault_log:
            import faulthandler
            faulthandler.disable()
            self.fault_log.close()
            self.fault_log = None


def child_env():
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE"):
        env.pop(key, None)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1")
    return env


def launch_context(mode=None):
    """Small, portable process-origin record for abrupt-exit diagnosis."""
    context = {
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "platform": sys.platform,
        "launch_mode": mode or os.environ.get("CC_LAUNCH_MODE", "unknown"),
    }
    if os.name != "nt":
        context.update(session_id=os.getsid(0), process_group=os.getpgrp())
    return context


def load_dotenv():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
