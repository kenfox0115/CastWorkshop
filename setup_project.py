#!/usr/bin/env python3
"""
Setup script for CastWorkshop.

- Checks for Ollama installation (installs if missing).
- Detects existing ComfyUI installation (desktop or other) and uses it.
- Sets up virtual environment and installs Python dependencies.
- Initializes SQLite DB if not present.
- Generates .env template with required env vars.
- Optionally starts the stack.

Run with: python setup_project.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("CastWorkshop requires Python 3.11 or newer.")

if __name__ == "__main__" and "--setup-log-child" not in sys.argv:
    from scripts.setup_logging import run_logged
    sys.exit(run_logged(__file__))

from scripts.model_manifest import load_manifest, readiness
from scripts.comfy_setup import offer_install
from scripts.runtime import load_dotenv

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parent  # CastWorkshop root
VENV_DIR = PROJECT_ROOT / ".venv"
REQ_VPS = PROJECT_ROOT / "vps" / "requirements.txt"
REQ_WORKER = PROJECT_ROOT / "worker" / "requirements.txt"
DB_PATH = PROJECT_ROOT / "vps" / "jobs.db"
SCHEMA_SQL = PROJECT_ROOT / "vps" / "schema.sql"
DOT_ENV = PROJECT_ROOT / ".env"

# Custom node paths are discovered from the detected ComfyUI install (below),
# NOT hardcoded to a specific user's machine. A bundled vendor copy may sit in
# <project>/third_party/ for the project's own pinned dependency set.
CUSTOM_NODES_PATH = None  # resolved after ComfyUI detection

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run_cmd(cmd, capture_output=False):
    """Run a shell command, optionally capturing stdout."""
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if capture_output:
        return result.stdout.strip()
    else:
        print(result.stdout)
        return result.returncode


def ollama_is_installed():
    """Return True if the `ollama` command is on PATH."""
    import shutil
    return shutil.which("ollama") is not None


def comfyui_is_running():
    """Check the configured ComfyUI API without assuming a particular port."""
    from urllib import request
    try:
        url = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
        with request.urlopen(url + "/system_stats", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def check_existing_comfyui():
    """Return a ComfyUI root from configuration or common portable locations."""
    home = Path.home()

    configured = os.environ.get("COMFYUI_PATH")
    possible_paths = ([Path(configured).expanduser()] if configured else []) + [
        PROJECT_ROOT / "ComfyUI",
        home / "ComfyUI",
        home / "comfyui",
        home / "Documents" / "ComfyUI",
    ]
    # ComfyUI Desktop (Windows) — via env-computed path, not a hardcoded user.
    appdata_local = os.environ.get("LOCALAPPDATA")
    if appdata_local:
        desktop_installs = Path(appdata_local) / "Comfy-Desktop" / "ComfyUI-Installs"
        if desktop_installs.is_dir():
            possible_paths.extend(desktop_installs.glob("*/ComfyUI"))
            possible_paths.append(desktop_installs / "ComfyUI")

    for path in possible_paths:
        for candidate in (path, path / "ComfyUI"):
            if (candidate / "main.py").is_file():
                resolved = candidate.resolve()
                print(f"[setup] Found existing ComfyUI installation at: {resolved}")
                return str(resolved)
    return None


def check_and_offer_models(comfy_running):
    """Report required models and optionally pull the configured Ollama model."""
    model = os.environ.get("OLLAMA_MODEL", "qwen3-14b-64k")
    ollama_manifest = next((
        item for item in load_manifest()["models"]
        if item["engine"] == "ollama" and item["name"] == model
    ), {"name": model})
    report = readiness(comfy_url=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"),
                       ollama_model=model)
    ollama_item = next(item for item in report["models"] if item["engine"] == "ollama")
    print(f"[setup] Ollama model {model}: {ollama_item['status']}")
    if report.get("errors", {}).get("ollama"):
        print("[setup][warn] Ollama API is unavailable. Start Ollama so it listens on "
              "127.0.0.1:11434, then rerun setup or inspect_env.py.")
    comfy_items = [item for item in report["models"] if item["engine"] == "comfyui"]
    present = sum(item["status"] == "present" for item in comfy_items)
    missing = [item["name"] for item in comfy_items if item["status"] == "missing"]
    print(f"[setup] ComfyUI model readiness: {present}/{len(comfy_items)} detected.")
    if not comfy_running:
        print("[setup][warn] ComfyUI API is unavailable. Start its server so it listens "
              "on 127.0.0.1:8188 before generating media.")
    if missing and comfy_running:
        print("[setup][warn] Missing ComfyUI models: " + ", ".join(missing))
    print("[setup] Optional image and video/voice dependency installation is offered separately below.")
    if (ollama_is_installed() and ollama_item["status"] == "missing" and sys.stdin.isatty()
            and input(f"  Install Ollama model {model} now? [y/N]: ").strip().lower() in ("y", "yes")):
        source = ollama_manifest.get("source") or model
        print(f"[setup] Downloading public Ollama source: {source}")
        result = subprocess.run(["ollama", "pull", source], check=False)
        if result.returncode:
            print(f"[setup][warn] Ollama could not download {source}; "
                  f"you can retry with: ollama pull {source}")
        elif source != model:
            recipe = PROJECT_ROOT / ollama_manifest["modelfile"]
            print(f"[setup] Creating local model alias: {model}")
            result = subprocess.run(
                ["ollama", "create", model, "-f", str(recipe)], check=False
            )
            if result.returncode:
                print(f"[setup][warn] Download succeeded, but the local alias could not be "
                      f"created. Retry with: ollama create {model} -f {recipe}")


def choose_network_host():
    """Preserve explicit configuration; otherwise offer local-only or trusted LAN."""
    configured = os.environ.get("API_HOST", "").strip()
    if not configured and DOT_ENV.exists():
        for line in DOT_ENV.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "API_HOST":
                configured = value.strip().strip("\"'")
    if configured:
        host = configured
    elif sys.stdin.isatty():
        print("\nNetwork access")
        print("  1. This computer only (recommended)")
        print("  2. Allow access from other devices on my local/trusted network")
        while True:
            try:
                answer = input("  Choose [1]: ").strip()
            except EOFError:
                answer = "1"
            if answer in ("", "1", "2"):
                host = "0.0.0.0" if answer == "2" else "127.0.0.1"
                break
            print("  Enter 1 or 2.")
    else:
        host = "127.0.0.1"
    if host == "0.0.0.0" and sys.stdin.isatty():
        print("CastWorkshop has no built-in login or HTTPS. Anyone who can reach this "
              "computer on the network may be able to access the interface and generated "
              "content. Use this only on a trusted home/LAN/Tailscale network. "
              "Do not expose port 8787 directly to the public internet.")
    return host


def generate_env_file(comfy_path=None, comfyui_running=False, comfy_python=None, ffmpeg=None):
    """Create a .env template if one is not already present.

    On first run the user is PROMPTED for the Telegram bot token (from
    @BotFather) rather than writing a placeholder, so no key ships and the bot
    is ready to run immediately after setup.
    """
    api_host = choose_network_host()
    os.environ["API_HOST"] = api_host
    if DOT_ENV.exists():
        # Save network/helper settings only where the existing value is absent or blank.
        # Never replace a token, service endpoint or explicit user configuration.
        available = {"API_HOST": api_host, "COMFYUI_PATH": comfy_path, "COMFY_PYTHON": comfy_python, "FFMPEG_PATH": ffmpeg,
                     "COMFY_MODELS_DIR": os.environ.get("COMFY_MODELS_DIR", "")}
        lines = DOT_ENV.read_text(encoding="utf-8-sig").splitlines()
        seen, changed = set(), False
        for index, line in enumerate(lines):
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            seen.add(key)
            if key in available and available[key] and not value.strip().strip('\"\''):
                lines[index] = f"{key}={available[key]}"
                changed = True
        for key, value in available.items():
            if value and key not in seen:
                lines.append(f"{key}={value}")
                changed = True
        if changed:
            temporary = DOT_ENV.with_name(DOT_ENV.name + ".setup.tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temporary.replace(DOT_ENV)
        print("[setup] Existing .env settings preserved; missing network/helper settings saved where available.")
        return

    print("[setup] Generating .env ...")

    # Prompt interactively for the secret. The value is never printed back.
    import getpass
    import inspect
    masked = "echo_char" in inspect.signature(getpass.getpass).parameters
    print("  Telegram is optional. Paste a token and press Enter, or leave it blank for web-only mode. " +
          ("Each character appears as *." if masked else
           "Input is hidden; confirmation appears after Enter."))
    if sys.stdin.isatty():
        token = getpass.getpass(
            "  Telegram bot token from @BotFather (optional): ",
            **({"echo_char": "*"} if masked else {}),
        ).strip()
    else:
        # Headless setup must not block or raise EOF waiting for an optional
        # secret. An explicitly supplied environment token still takes effect.
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        print("[setup] Token received (value hidden).")
        if ":" not in token:
            print("[setup][warn] That doesn't look like a Telegram token "
                  "(expected '123456789:AA...'). It will be saved as-is.")
    else:
        print("[setup] Telegram skipped. You can configure it later in web Settings.")

    example = PROJECT_ROOT / ".env.example"
    if not example.is_file():
        raise FileNotFoundError("Missing .env.example; setup cannot create a safe configuration")
    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ENABLED": "1" if token else "0",
        "API_HOST": api_host,
        "COMFYUI_PATH": comfy_path or "",
        "COMFY_MODELS_DIR": os.environ.get("COMFY_MODELS_DIR", ""),
        "COMFY_PYTHON": comfy_python or os.environ.get("COMFY_PYTHON", ""),
        "FFMPEG_PATH": ffmpeg or os.environ.get("FFMPEG_PATH", ""),
    }
    lines = []
    for line in example.read_text(encoding="utf-8-sig").splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        lines.append(f"{key}={values[key]}" if key in values else line)
    template = "\n".join(lines)
    DOT_ENV.write_text(template.strip() + "\n", encoding="utf-8")
    print("[setup] .env created at ./.env (file is git-ignored).")


# --------------------------------------------------------------------------- #
# Installation steps
# --------------------------------------------------------------------------- #
def setup_virtualenv():
    """Create a Python virtual environment and install project dependencies."""
    if VENV_DIR.exists():
        print("[setup] Virtual environment already exists – reusing.")
    else:
        print("[setup] Creating virtual environment...")
        import venv
        venv.create(VENV_DIR, with_pip=True)

    # Use python.exe directly from venv
    python_exe = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python_exe.exists():
        print("[setup][error] Could not locate venv python interpreter.")
        sys.exit(1)

    print("[setup] Upgrading pip in virtual environment...")
    subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], check=True)

    for req_path in (REQ_VPS, REQ_WORKER):
        if req_path.exists():
            print(f"[setup] Installing requirements from {req_path}...")
            # Worker requirements include the PyAV compatibility cap needed by
            # faster-whisper; pip also corrects an out-of-range installed PyAV
            # when setup is rerun in an existing virtual environment.
            subprocess.run(
                [str(python_exe), "-m", "pip", "install", "-r", str(req_path)], check=True
            )
    prepare_voice_trimming(python_exe)


def prepare_voice_trimming(python_exe):
    """Prepare worker audio tools in the application environment, not ComfyUI."""
    print("[setup] Preparing voice trimming (CPU recognition model and FFmpeg)...")
    try:
        subprocess.run([str(python_exe), "-m", "worker.voice_trim", "--prepare"],
                       cwd=PROJECT_ROOT, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("[setup][warn] Voice trimming is not ready. Check the error above and rerun setup with network access.")
        return False
    return True


def initialize_db():
    """Create the SQLite DB and run migrations if it does not exist."""
    if DB_PATH.exists():
        _migrate_existing_db()
        print("[setup] Database already exists – migrations checked.")
        return

    print("[setup] Initializing SQLite database...")
    os.makedirs(PROJECT_ROOT / "vps", exist_ok=True)

    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    with open(SCHEMA_SQL, "r") as f:
        conn.executescript(f.read())
    conn.close()
    print("[setup] Database initialized.")


def _migrate_existing_db():
    """Idempotent column migrations for databases created before a schema fix.
    SQLite cannot ALTER-add NOT NULL without default, so ADD COLUMN with the
    same defaults the schema declares. No-ops when columns already exist."""
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if cols and "priority" not in cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 5"
            )
            print("[setup] Migrated: jobs.priority added")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(characters)")}
        if cols and "voice_sample_path" not in cols:
            conn.execute(
                "ALTER TABLE characters ADD COLUMN voice_sample_path TEXT"
            )
            print("[setup] Migrated: characters.voice_sample_path added")
        conn.execute("""CREATE TABLE IF NOT EXISTS character_scenes (
            character_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            description TEXT NOT NULL DEFAULT '',
            anchor_artifact_id INTEGER,
            anchor_reference_image TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
            FOREIGN KEY (anchor_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
        )""")
        scene_cols = {r[1] for r in conn.execute("PRAGMA table_info(character_scenes)")}
        if "anchor_artifact_id" not in scene_cols:
            conn.execute("ALTER TABLE character_scenes ADD COLUMN anchor_artifact_id INTEGER")
        if "anchor_reference_image" not in scene_cols:
            conn.execute("ALTER TABLE character_scenes ADD COLUMN anchor_reference_image TEXT")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Main orchestration
# --------------------------------------------------------------------------- #
def offer_start():
    """Launch through the normal entry point only after an interactive opt-in."""
    if not sys.stdin.isatty():
        return 0
    try:
        answer = input("  Start CastWorkshop now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n[setup] Start later with: python start.py")
        return 0
    if answer not in ("y", "yes"):
        return 0
    try:
        if (os.environ.get("CASTWORKSHOP_SETUP_INTERACTIVE") == "1"
                and os.environ.get("OPEN_WEB_UI", "auto").lower() == "auto"):
            os.environ["OPEN_WEB_UI"] = "1"
        return subprocess.run([sys.executable, str(PROJECT_ROOT / "start.py")],
                              cwd=PROJECT_ROOT, check=False).returncode
    except OSError as exc:
        print(f"[setup][warn] Could not launch start.py: {exc}")
        return 1


def main():
    load_dotenv()
    print("=== CastWorkshop Setup ===")
    print(f"Project root: {PROJECT_ROOT}")
    print()

    # 1️⃣ Ensure Ollama is installed
    if not ollama_is_installed():
        print("[setup] Ollama not found – please install it manually from https://ollama.com")
    else:
        print("[setup] Ollama already installed.")

    # 2️⃣ Check for existing ComfyUI installation
    existing_comfy_path = check_existing_comfyui()
    if not existing_comfy_path and sys.stdin.isatty():
        entered = input("  ComfyUI folder containing main.py (blank to skip): ").strip().strip('"')
        if entered:
            candidate = Path(entered).expanduser()
            if (candidate / "main.py").is_file():
                existing_comfy_path = str(candidate.resolve())
            else:
                print("[setup][warn] That folder does not contain main.py; dependency installation will be skipped.")
    comfy_running = False

    if existing_comfy_path:
        print(f"[setup] Detected ComfyUI installation at: {existing_comfy_path}")
        print("[setup] Using existing ComfyUI instance.")
        comfy_running = comfyui_is_running()
        if not comfy_running:
            print("[setup] Warning: ComfyUI is not running (port 8188 not responding)")
    elif not comfyui_is_running():
        print("[setup] No existing ComfyUI detected.")
        print("[setup] Skipping ComfyUI installation (manual installation recommended).")
    else:
        print("[setup] ComfyUI already running.")
        comfy_running = True

    # 3️⃣ Install custom nodes if ComfyUI is present
    # Install application dependencies (including certifi) before HTTPS downloads.
    setup_virtualenv()
    # Offer Ollama first, then independently selected ComfyUI dependency groups.
    check_and_offer_models(comfy_running)
    dependencies = offer_install(existing_comfy_path, os.environ.get("COMFY_URL", "http://127.0.0.1:8188"))

    # 5️⃣ Initialize SQLite DB
    initialize_db()

    # 6️⃣ Generate .env template
    generate_env_file(existing_comfy_path, comfy_running,
                      comfy_python=dependencies.get("python") if dependencies else None,
                      ffmpeg=dependencies.get("ffmpeg") if dependencies else None)

    # --------------------------------------------------------------- #
    # Final instructions
    # --------------------------------------------------------------- #
    print("\n" + "="*50)
    print("=== Setup complete ===")
    if dependencies and dependencies["failures"]:
        print("[setup][warn] Some ComfyUI dependencies need attention; see the download warnings above.")
    print()
    print("Next steps:")
    print("  1. Start Ollama and restart ComfyUI if setup installed custom nodes")
    print("  2. Review docs/MODELS.md if ComfyUI reports missing models or nodes")
    print("  3. Run: python start.py")
    print("  Ollama and ComfyUI only need local API access on this machine.")
    print(f"  Network binding: {os.environ.get('API_HOST', '127.0.0.1')}. "
          "Change API_HOST in .env and restart to change network access.")
    print()

    if existing_comfy_path:
        print(f"Using ComfyUI at: {existing_comfy_path}")
        if not comfy_running:
            print("WARNING: Start ComfyUI Desktop and click 'Start Server' first!")

    start_result = offer_start()
    return start_result or (1 if dependencies and dependencies["failures"] else 0)


if __name__ == "__main__":
    sys.exit(main())
