"""Canonical, portable, offline verification gate for CastWorkshop."""
from __future__ import annotations

import json
from pathlib import Path
import py_compile
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (ROOT / "vps", ROOT / "worker", ROOT / "scripts", ROOT / "tests")
ROOT_MODULES = (ROOT / "start.py", ROOT / "stop.py", ROOT / "restart.py", ROOT / "setup_project.py")


def python_files() -> list[Path]:
    files = list(ROOT_MODULES)
    for directory in SOURCE_DIRS:
        files.extend(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(set(files))


def workflow_files() -> list[Path]:
    return sorted((ROOT / "worker" / "workflows").rglob("*.json"))


def check_python() -> None:
    files = python_files()
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(f"[verify] Python compilation: {len(files)} files passed")


def check_workflows() -> None:
    files = workflow_files()
    for path in files:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not value:
            raise ValueError(f"workflow must be a non-empty JSON object: {path.relative_to(ROOT)}")
    print(f"[verify] Workflow JSON: {len(files)} files passed")


def run(label: str, args: list[str]) -> None:
    print(f"[verify] {label}...", flush=True)
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", *args], cwd=ROOT)
    if result.returncode:
        raise SystemExit(result.returncode)
    print(f"[verify] {label}: passed")


def main() -> int:
    check_python()
    check_workflows()
    run("Smoke tests", ["tests/smoke_test.py"])
    run("Unit tests", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])
    print("[verify] All offline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
