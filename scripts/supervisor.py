"""Own direct children, restart only observed exits, never kill unrelated processes."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime import (ROOT, LOG_DIR, STATE_FILE, STOP_FILE, InstanceLock,
                             Heartbeat, atomic_json, read_json, stack_identity,
                             child_env, load_dotenv, launch_context)


def resolve_db_path(value):
    path = Path(value or "vps/jobs.db")
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def healthy(url, expected_pid=None):
    try:
        # This is a child owned by this supervisor. Ignore OS proxy settings,
        # which can route a healthy loopback service away from the VM.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as response:
            return response.status == 200 and (expected_pid is None or json.load(response).get("pid") == expected_pid)
    except Exception:
        return False


def local_health_url(host, port):
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/health"


def child_pid(name, proc, launched_at, max_age=None):
    """Identify the app process behind a Windows virtualenv launcher."""
    beat = read_json(LOG_DIR / (name + ".heartbeat"))
    pid, at = beat.get("pid"), beat.get("at", 0)
    if not isinstance(pid, int) or not isinstance(at, (int, float)) or at < launched_at:
        return None
    if max_age is not None and time.time() - at > max_age:
        return None
    if pid == proc.pid or beat.get("parent_pid") == proc.pid:
        return pid
    return None


def child_healthy(name, proc, launched_at, url):
    pid = child_pid(name, proc, launched_at)
    return pid is not None and healthy(url, pid)


def stop_child(proc, actual_pid=None):
    if actual_pid and actual_pid != proc.pid:
        try:
            os.kill(actual_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logging.warning("Could not stop child pid=%s: %s", actual_pid, type(exc).__name__)
    if proc.poll() is None:
        proc.terminate()  # exact child handle; no PID lookup, wrapper, or global sweep
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def run():
    load_dotenv()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "supervisor.log", maxBytes=5_000_000,
                                  backupCount=3, encoding="utf-8")
    logging.basicConfig(handlers=[handler], level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    lock = InstanceLock(stack_identity())
    if not lock.acquire():
        logging.error("Supervisor already running for this directory")
        return 2
    previous = read_json(STATE_FILE)
    if previous:
        evidence = {"state": previous, "heartbeats": {
            name: read_json(LOG_DIR / (name + ".heartbeat"))
            for name in ("supervisor", "vps", "worker")}}
        atomic_json(LOG_DIR / ("previous-run-" + uuid.uuid4().hex + ".json"), evidence)
        logging.warning("Previous run left state behind; preserved exit evidence for pid=%s", previous.get("supervisor"))
    run_id = uuid.uuid4().hex
    stopping = {"requested": False, "signal": None}

    def request_stop(signum, _frame):
        stopping.update(requested=True, signal=signum)

    for signal_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    context = launch_context()
    atomic_json(LOG_DIR / "supervisor-context.json", context)
    logging.info("Supervisor context %s", json.dumps(context, sort_keys=True))
    children, outputs, retries, next_start = {}, {}, {}, {}
    started_at, launched_at = {}, {}
    last_warning = {}
    heartbeat = Heartbeat("supervisor").start()
    api_port = int(os.environ.get("API_PORT", "8787"))
    api_host = os.environ.get("API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    vps_url = os.environ.get("VPS_URL", f"http://127.0.0.1:{api_port}")
    health_url = local_health_url(api_host, api_port)
    env = child_env()
    env["DB_PATH"] = str(resolve_db_path(env.get("DB_PATH")))
    env["API_PORT"] = str(api_port)
    env["VPS_URL"] = vps_url
    specs = {"vps": ROOT / "vps" / "app.py"}
    if os.environ.get("WORKER_ENABLED", "1") == "1":
        specs["worker"] = ROOT / "worker" / "worker.py"
    try:
        # Refuse to replace an existing server, including an older unmodified copy.
        with socket.socket() as probe:
            probe.bind((api_host, api_port))
        STOP_FILE.unlink(missing_ok=True)
        atomic_json(STATE_FILE, {"run_id": run_id, "supervisor": os.getpid(),
                               "root": str(ROOT), "status": "starting",
                               "launch": context})
        while True:
            if stopping["requested"]:
                logging.info("Stop signal received: %s", stopping["signal"])
                return 0
            if read_json(STOP_FILE).get("run_id") == run_id:
                logging.info("Stop requested for %s", run_id)
                return 0
            now = time.monotonic()
            for name, script in specs.items():
                proc = children.get(name)
                if proc is not None and proc.poll() is not None:
                    code = proc.returncode
                    logging.error("%s exited code=%s pid=%s", name, code, proc.pid)
                    outputs.pop(name).close()
                    children.pop(name)
                    launched_at.pop(name, None)
                    retries[name] = 0 if now - started_at[name] > 300 else retries.get(name, 0) + 1
                    if retries[name] >= 5:
                        raise RuntimeError(f"{name} exited repeatedly; stopping stack for diagnosis")
                    next_start[name] = now + min(60, 2 ** retries[name])
                    proc = None
                if proc is None and now >= next_start.get(name, 0):
                    if name == "worker" and (
                        "vps" not in children or
                        not child_healthy("vps", children["vps"], launched_at["vps"], health_url)
                    ):
                        continue
                    path = LOG_DIR / (name + ".log")
                    if path.exists() and path.stat().st_size > 10_000_000:
                        os.replace(path, path.with_suffix(".log.previous"))
                    out = open(path, "a", encoding="utf-8")
                    try:
                        launch_time = time.time()
                        proc = subprocess.Popen([sys.executable, str(script)], cwd=script.parent,
                            env=env, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    except Exception:
                        out.close()
                        raise
                    children[name], outputs[name], started_at[name] = proc, out, now
                    launched_at[name] = launch_time
                    logging.info("%s started pid=%s", name, proc.pid)
                if proc is not None and now - started_at.get(name, now) > 60:
                    beat = read_json(LOG_DIR / (name + ".heartbeat"))
                    stale = (child_pid(name, proc, launched_at[name]) is None or
                             time.time() - beat.get("at", 0) > 60)
                    if stale and now - last_warning.get(name, 0) > 60:
                        logging.warning("%s pid=%s has stale liveness data (heartbeat pid=%s, age=%.1fs); leaving it alive for diagnosis",
                                        name, proc.pid, beat.get("pid"),
                                        time.time() - beat.get("at", 0))
                        last_warning[name] = now
            # Publish only application PIDs confirmed by this launch's heartbeat.
            # Windows venv redirectors have a different PID from their child;
            # publishing the launcher first makes ownership appear to change.
            confirmed = {name: pid for name, proc in children.items()
                         if (pid := child_pid(name, proc, launched_at[name])) is not None}
            state = {"run_id": run_id, "supervisor": os.getpid(), "root": str(ROOT),
                     "status": "running" if set(confirmed) == set(specs) and child_healthy(
                         "vps", children["vps"], launched_at["vps"], health_url) else "starting",
                     "children": confirmed,
                     "launch": context}
            atomic_json(STATE_FILE, state)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.exception("Supervisor stopped")
        return 1
    finally:
        logging.info("Supervisor cleanup pid=%s run=%s", os.getpid(), run_id)
        for name in ("worker", "vps"):
            if name in children:
                stop_child(children[name], child_pid(
                    name, children[name], launched_at[name], max_age=15))
        for out in outputs.values():
            out.close()
        if read_json(STATE_FILE).get("run_id") == run_id:
            STATE_FILE.unlink(missing_ok=True)
        if read_json(STOP_FILE).get("run_id") == run_id:
            STOP_FILE.unlink(missing_ok=True)
        heartbeat.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(run())
