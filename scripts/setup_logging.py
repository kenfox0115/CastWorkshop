"""Capture setup and inherited subprocess output without logging user input."""
import codecs
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys

CHILD_FLAG = "--setup-log-child"


def run_logged(script):
    script = Path(script).resolve()
    project_root = script.parent.parent if script.parent.name == "scripts" else script.parent
    log_path = project_root / "logs" / "setup.log"
    interactive = sys.stdin.isatty()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"[setup][error] Cannot create setup log: {exc}", file=sys.stderr)
        if interactive:
            _pause()
        return 1
    with log:
        def emit(message):
            log.write(message)
            log.flush()
            try:
                sys.stdout.write(message)
                sys.stdout.flush()
            except UnicodeEncodeError:
                sys.stdout.write(message.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8"))
                sys.stdout.flush()

        emit(f"\n=== Setup started {datetime.now().isoformat(timespec='seconds')} ===\n[setup] Log: {log_path}\n")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["CASTWORKSHOP_SETUP_INTERACTIVE"] = "1" if interactive and sys.stdout.isatty() else "0"
        code = 1
        try:
            with subprocess.Popen([sys.executable, "-u", str(script), CHILD_FLAG],
                                  cwd=project_root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while True:
                    chunk = os.read(child.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    emit(decoder.decode(chunk))
                emit(decoder.decode(b"", final=True))
                code = child.wait()
        except OSError as exc:
            emit(f"[setup][error] Could not run setup: {exc}\n")
        except KeyboardInterrupt:
            emit("\n[setup] Interrupted by user.\n")
            code = 130
        emit(f"\n[setup] Exit code: {code}. Log saved to: {log_path}\n")
        if code and interactive:
            _pause()
        return code


def _pause():
    try:
        input("Setup failed. Press Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass
