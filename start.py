"""Start this copy through its single owning supervisor."""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from scripts.runtime import (ROOT, LOG_DIR, STATE_FILE, InstanceLock, stack_identity,
                             read_json, child_env, load_dotenv, atomic_json,
                             launch_context, project_python)


def supervisor_launch_options():
    """Detach from the invoking terminal/session on every supported platform."""
    if os.name != "nt":
        return {
            "stdin": subprocess.DEVNULL,
            "start_new_session": True,
            "launch_mode": "posix-new-session",
        }
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    )
    return {
        "stdin": subprocess.DEVNULL,
        "creationflags": flags,
        "launch_mode": "windows-job-breakaway",
    }


def launch_supervisor(out, env=None, popen=subprocess.Popen):
    """Launch with job breakaway when allowed; record a safe Windows fallback."""
    env = child_env() if env is None else dict(env)
    options = supervisor_launch_options()
    mode = options.pop("launch_mode")
    env["CC_LAUNCH_MODE"] = mode
    command = [project_python(), str(ROOT / "scripts/supervisor.py")]
    try:
        return popen(command, cwd=ROOT, env=env, stdout=out,
                     stderr=subprocess.STDOUT, **options), mode
    except OSError as exc:
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if os.name != "nt" or not (options.get("creationflags", 0) & breakaway):
            raise
        # Some Windows host jobs prohibit breakaway. Keep startup functional and
        # make the weaker boundary visible rather than silently claiming isolation.
        options["creationflags"] &= ~breakaway
        mode = f"windows-detached-fallback-{getattr(exc, 'winerror', 'error')}"
        env["CC_LAUNCH_MODE"] = mode
        return popen(command, cwd=ROOT, env=env, stdout=out,
                     stderr=subprocess.STDOUT, **options), mode


def web_url():
    host = os.environ.get("API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{os.environ.get('API_PORT','8787')}/web/"


def web_access_urls(address_lookup=socket.getaddrinfo):
    """Return local and discoverable LAN URLs for the configured binding."""
    urls = [web_url()]
    host = os.environ.get("API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host not in ("0.0.0.0", "::"):
        return urls
    try:
        records = address_lookup(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        records = []
    port = os.environ.get("API_PORT", "8787")
    addresses = sorted({record[4][0] for record in records})
    for address in addresses:
        if address.startswith("127.") or address.startswith("169.254."):
            continue
        url = f"http://{address}:{port}/web/"
        if url not in urls:
            urls.append(url)
    return urls


def print_web_locations():
    urls = web_access_urls()
    print(f"[start] Web interface: {urls[0]}")
    for url in urls[1:]:
        print(f"[start] Network access: {url}")
    if len(urls) == 1 and (os.environ.get("API_HOST", "127.0.0.1").strip() or "127.0.0.1") in ("0.0.0.0", "::"):
        print("[start] Network access is enabled; use this computer's LAN address on another device.")


def open_web_interface(opener=webbrowser.open,interactive=None):
    preference = os.environ.get("OPEN_WEB_UI", "auto").strip().lower()
    if preference in ("0", "false", "no", "off"):
        return False
    if interactive is None:
        interactive = sys.stdout.isatty()
    if preference not in ("1", "true", "yes", "on") and not interactive:
        return False
    url = web_url()
    try:
        opened = bool(opener(url, new=2))
        return opened
    except (OSError, webbrowser.Error) as exc:
        print(f"[start] Could not open a browser ({type(exc).__name__}); visit {url}")
        return False


def owns_supervisor(state, launcher_pid):
    """Accept the direct PID or the child of a Windows venv redirector."""
    context = state.get("launch") or {}
    supervisor_pid = state.get("supervisor")
    return (supervisor_pid == launcher_pid or
            (context.get("pid") == supervisor_pid and
             context.get("parent_pid") == launcher_pid))


def main():
    load_dotenv()
    try:
        project_python()
    except FileNotFoundError as exc:
        print(f"[start] {exc}")
        return 1
    if os.environ.get("TELEGRAM_ENABLED", "1" if os.environ.get("TELEGRAM_BOT_TOKEN") else "0") == "1" and not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("[start] TELEGRAM_BOT_TOKEN is missing")
        return 3
    lock = InstanceLock(stack_identity())
    if not lock.acquire():
        print("[start] This copy already has a supervisor. Opening its web interface.")
        print_web_locations()
        open_web_interface()
        return 1
    lock.close()  # Supervisor takes the authoritative lock; concurrent launches cannot both own it.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "supervisor-startup.log", "a", encoding="utf-8") as out:
        proc, launch_mode = launch_supervisor(out)
    atomic_json(LOG_DIR / "launch-context.json", {
        "launcher": launch_context("foreground-launcher"),
        "supervisor_pid": proc.pid,
        "requested_boundary": launch_mode,
        "started_at": time.time(),
    })
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print("[start] Supervisor exited; inspect logs/supervisor.log and logs/vps.log")
            return 1
        state = read_json(STATE_FILE)
        if owns_supervisor(state, proc.pid) and state.get("status") == "running":
            print(f"[start] API ready; supervisor pid {state['supervisor']}, root {ROOT}, boundary {launch_mode}")
            print_web_locations()
            open_web_interface()
            return 0
        time.sleep(0.25)
    print("[start] Startup is not ready; inspect logs/vps.log, logs/vps-internal.log, "
          "and logs/supervisor.log (the supervisor may still be starting)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
