"""
restart.py — Restart the CastWorkshop VPS + worker stack.

Runs stop.py then start.py. Equivalent to stop && start.
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def run(script: str) -> int:
    p = subprocess.run([sys.executable, os.path.join(BASE, script)],
                       capture_output=True, text=True)
    if p.stdout:
        print(p.stdout, end="")
    if p.stderr and p.returncode != 0:
        print(p.stderr, end="")
    return p.returncode


def main() -> int:
    print("=== Restarting CastWorkshop stack ===")
    rc = run("stop.py")
    if rc != 0:
        print("[restart] Stop was not confirmed; refusing to start a second instance.")
        return rc
    rc = run("start.py")
    if rc != 0:
        print(f"[restart] start.py returned {rc}.")
        return rc
    print("=== Restart complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
