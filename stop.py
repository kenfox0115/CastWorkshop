"""Stop this copy's supervisor and clean up only its orphaned app processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from scripts.runtime import (ROOT, STATE_FILE, STOP_FILE, InstanceLock,
                             stack_identity, read_json, atomic_json)


PROCESS_SCRIPTS = {
    "supervisor": Path("scripts") / "supervisor.py",
    "vps": Path("vps") / "app.py",
    "worker": Path("worker") / "worker.py",
    "launcher": Path("start.py"),
}


def _windows_processes():
    """Return Python processes whose command line runs a script from this copy."""
    markers = [str((ROOT / relative).resolve()).replace("/", "\\")
               for relative in PROCESS_SCRIPTS.values()]
    ps_markers = ",".join("'" + marker.replace("'", "''") + "'" for marker in markers)
    script = rf"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$markers = @({ps_markers})
try {{
    $found = @(Get-CimInstance -ClassName Win32_Process | Where-Object {{
        if ($_.Name -notlike 'python*.exe') {{ return $false }}
        $line = $_.CommandLine
        if ([string]::IsNullOrEmpty($line)) {{ return $false }}
        $line = $line.Replace('/', '\')
        foreach ($marker in $markers) {{
            if ($line.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {{ return $true }}
        }}
        return $false
    }} | Select-Object ProcessId, ParentProcessId, CommandLine)
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
}}
if ($found.Count -gt 0) {{ ConvertTo-Json -InputObject $found -Compress }}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"Could not inspect Windows processes: {detail}")
    output = result.stdout.strip()
    if not output:
        return []
    records = json.loads(output)
    if isinstance(records, dict):
        records = [records]
    return [_record(item["ProcessId"], item.get("ParentProcessId"),
                    item.get("CommandLine", "")) for item in records]


def _posix_processes():
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=10,
    )
    if result.returncode:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"Could not inspect processes: {detail}")
    markers = {kind: str((ROOT / relative).resolve())
               for kind, relative in PROCESS_SCRIPTS.items()}
    records = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid, parent_pid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        command = fields[2]
        for kind, marker in markers.items():
            if marker in command:
                records.append(_record(pid, parent_pid, command, kind))
                break
    return records


def _record(pid, parent_pid, command, kind=None):
    if kind is None:
        normalized = command.replace("/", "\\").casefold()
        for role, relative in PROCESS_SCRIPTS.items():
            marker = str((ROOT / relative).resolve()).replace("/", "\\").casefold()
            if marker in normalized:
                kind = role
                break
    return {"pid": int(pid), "parent_pid": parent_pid,
            "command": command, "kind": kind}


def find_processes():
    return _windows_processes() if os.name == "nt" else _posix_processes()


def _signal_processes(records, sig):
    signalled = []
    for record in records:
        pid = record["pid"]
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, sig)
            signalled.append(pid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            print(f"[stop] Could not stop pid {pid}: {type(exc).__name__}")
    return signalled


def _wait_for_processes(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = find_processes()
        if not current:
            return []
        time.sleep(0.25)
    return find_processes()


def _request_supervisor_stop(probe):
    """Request graceful shutdown while the instance lock is owned."""
    state = read_json(STATE_FILE)
    if state.get("root") != str(ROOT) or not state.get("run_id"):
        print("[stop] Supervisor owns the lock but its state is unavailable; will inspect this copy's processes.")
        return False
    atomic_json(STOP_FILE, {"run_id": state["run_id"]})
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if probe.acquire():
            probe.close()
            print("[stop] Supervisor completed shutdown.")
            return True
        time.sleep(0.25)
    print("[stop] Supervisor did not release its lock; will stop this copy's processes directly.")
    return False


def main():
    probe = InstanceLock(stack_identity())
    try:
        owns_lock = not probe.acquire()
        if not owns_lock:
            probe.close()
            print("[stop] No supervisor lock found; checking for orphaned processes from this copy.")
        else:
            _request_supervisor_stop(probe)

        processes = find_processes()
        if not processes:
            print("[stop] No supervisor, VPS, worker, or launcher processes found for this copy.")
            return 0

        supervisors = [p for p in processes if p["kind"] == "supervisor"]
        if supervisors:
            print("[stop] Stopping supervisor process(es): " + ", ".join(str(p["pid"]) for p in supervisors))
            _signal_processes(supervisors, signal.SIGTERM)
            remaining = _wait_for_processes(3)
            processes = remaining

        # A supervisor may have died without cleanup, or its children may have
        # outlived it. Stop only scripts whose absolute paths belong to ROOT.
        leftovers = processes
        if leftovers:
            print("[stop] Stopping orphaned process(es) from this copy: " +
                  ", ".join(f"{p['kind']} pid {p['pid']}" for p in leftovers))
            _signal_processes(leftovers, signal.SIGTERM)

        remaining = _wait_for_processes(5)
        if remaining and os.name != "nt":
            _signal_processes(remaining, signal.SIGKILL)
            remaining = _wait_for_processes(2)
        if remaining:
            print("[stop] Processes remain for this copy: " +
                  ", ".join(f"{p['kind']} pid {p['pid']}" for p in remaining))
            return 1

        # On Windows SIGTERM maps to TerminateProcess; process listing can lag
        # briefly, so the final query above is the success criterion.
        print("[stop] All processes for this copy have stopped.")
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"[stop] Could not safely inspect or stop this copy: {exc}")
        return 1
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())
