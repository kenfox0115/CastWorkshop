"""Offline process tests. Children only execute disposable test code."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import runtime
import start
from scripts.supervisor import stop_child
from scripts.supervisor import child_pid, local_health_url, resolve_db_path

class RuntimeTests(unittest.TestCase):
    def test_windows_venv_redirector_pid_is_owned_by_supervisor(self):
        state = {"supervisor": 320, "launch": {"pid": 320, "parent_pid": 1052}}
        self.assertTrue(start.owns_supervisor(state, 1052))
        self.assertFalse(start.owns_supervisor(state, 9999))
        self.assertTrue(start.owns_supervisor({"supervisor": 1052}, 1052))

    def test_heartbeat_identifies_actual_child_behind_redirector(self):
        with tempfile.TemporaryDirectory() as directory:
            beat = Path(directory) / "vps.heartbeat"
            beat.write_text('{"pid": 320, "parent_pid": 1052, "at": 200}', encoding="utf-8")
            proc = Mock(pid=1052)
            with patch("scripts.supervisor.LOG_DIR", Path(directory)):
                self.assertEqual(child_pid("vps", proc, 199), 320)
                self.assertIsNone(child_pid("vps", proc, 201))
                self.assertIsNone(child_pid("vps", Mock(pid=999), 199))

    def test_supervisor_probes_child_locally_even_when_listening_on_all_interfaces(self):
        self.assertEqual(local_health_url("0.0.0.0", 8787), "http://127.0.0.1:8787/health")
        self.assertEqual(local_health_url("::", 8787), "http://127.0.0.1:8787/health")
        self.assertEqual(local_health_url("192.168.1.10", 8787), "http://192.168.1.10:8787/health")

    def test_launcher_uses_setup_virtualenv_for_supervisor_and_children(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            interpreter = venv / ("Scripts/python.exe" if runtime.os.name == "nt" else "bin/python")
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            self.assertEqual(runtime.project_python(directory), str(interpreter))
            with patch.object(start, "project_python", return_value=str(interpreter)):
                launched = Mock(pid=42)
                popen = Mock(return_value=launched)
                proc, _mode = start.launch_supervisor(Mock(), env={}, popen=popen)
            self.assertIs(proc, launched)
            self.assertEqual(popen.call_args.args[0][0], str(interpreter))

    def test_incomplete_setup_environment_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".venv").mkdir()
            with self.assertRaisesRegex(FileNotFoundError, "Rerun python setup_project.py"):
                runtime.project_python(directory)

    def test_lock_excludes_second_process_and_releases(self):
        key = "test:" + uuid.uuid4().hex
        code = "from scripts.runtime import InstanceLock; import sys; x=InstanceLock(sys.argv[1]); ok=x.acquire(); x.close(); sys.exit(0 if ok else 2)"
        lock = runtime.InstanceLock(key)
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(subprocess.run([sys.executable, "-B", "-c", code, key], cwd=runtime.ROOT).returncode, 2)
        finally:
            lock.close()
        self.assertEqual(subprocess.run([sys.executable, "-B", "-c", code, key], cwd=runtime.ROOT).returncode, 0)

    def test_heartbeat_changes_during_blocking_work(self):
        with tempfile.TemporaryDirectory() as directory:
            beat = runtime.Heartbeat("test", interval=0.02)
            beat.path = Path(directory) / "beat"
            beat.start()
            before = runtime.read_json(beat.path)["at"]
            try:
                time.sleep(0.12)
                self.assertGreater(runtime.read_json(beat.path)["at"], before)
            finally:
                beat.close()

    def test_stop_owns_only_exact_child(self):
        a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            stop_child(a)
            self.assertIsNotNone(a.poll())
            self.assertIsNone(b.poll())
        finally:
            stop_child(a)
            stop_child(b)

    def test_endpoint_aliases_share_lock(self):
        self.assertEqual(runtime.resource_identity("http://localhost:8188"), runtime.resource_identity("http://127.0.0.1:8188"))

    def test_relative_database_path_is_root_relative(self):
        self.assertEqual(resolve_db_path("./vps/jobs.db"),
                         (runtime.ROOT / "vps/jobs.db").resolve())

    def test_posix_launcher_starts_a_new_session_with_no_stdin(self):
        with patch.object(start.os, "name", "posix"):
            options = start.supervisor_launch_options()
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["launch_mode"], "posix-new-session")

    def test_windows_launcher_requests_breakaway_and_has_recorded_fallback(self):
        fake = Mock(side_effect=[OSError("breakaway denied"), Mock(pid=77)])
        flags = {"DETACHED_PROCESS": 8, "CREATE_NEW_PROCESS_GROUP": 16,
                 "CREATE_BREAKAWAY_FROM_JOB": 32}
        with patch.object(start.os, "name", "nt"), \
             patch.multiple(start.subprocess, create=True, **flags):
            process, mode = start.launch_supervisor(Mock(), env={}, popen=fake)
        self.assertEqual(process.pid, 77)
        self.assertTrue(mode.startswith("windows-detached-fallback-"))
        self.assertEqual(fake.call_args_list[0].kwargs["creationflags"], 56)
        self.assertEqual(fake.call_args_list[1].kwargs["creationflags"], 24)

    def test_default_or_local_binding_prints_no_lan_url(self):
        for host in (None, "127.0.0.1", ""):
            with self.subTest(host=host), patch.dict(start.os.environ, {}, clear=True):
                if host is not None:
                    start.os.environ["API_HOST"] = host
                lookup = Mock()
                self.assertEqual(start.web_access_urls(lookup), ["http://127.0.0.1:8787/web/"])
                lookup.assert_not_called()
                self.assertEqual(runtime.child_env().get("API_HOST"), host)

    def test_lan_configuration_survives_child_environment_and_health_stays_local(self):
        with patch.dict(start.os.environ, {"API_HOST": "0.0.0.0"}, clear=True):
            self.assertEqual(runtime.child_env()["API_HOST"], "0.0.0.0")
            self.assertEqual(local_health_url("0.0.0.0", 8787), "http://127.0.0.1:8787/health")

    def test_web_url_is_loopback_even_when_server_binds_all_interfaces(self):
        with patch.dict(start.os.environ,{"API_HOST":"0.0.0.0","API_PORT":"9000"},clear=False):
            self.assertEqual(start.web_url(),"http://127.0.0.1:9000/web/")

    def test_web_access_urls_include_lan_addresses_for_wildcard_binding(self):
        records = [
            (start.socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
            (start.socket.AF_INET, 0, 0, "", ("192.168.1.42", 0)),
        ]
        with patch.dict(start.os.environ,{"API_HOST":"0.0.0.0","API_PORT":"9000"},clear=False):
            self.assertEqual(start.web_access_urls(lambda *_: records), [
                "http://127.0.0.1:9000/web/",
                "http://192.168.1.42:9000/web/",
            ])

    def test_browser_open_is_cross_platform_and_headless_aware(self):
        opener=Mock(return_value=True)
        with patch.dict(start.os.environ,{"OPEN_WEB_UI":"auto"},clear=False):
            self.assertFalse(start.open_web_interface(opener,interactive=False))
            self.assertTrue(start.open_web_interface(opener,interactive=True))
        opener.assert_called_once_with(start.web_url(),new=2)
        with patch.dict(start.os.environ,{"OPEN_WEB_UI":"0"},clear=False):
            self.assertFalse(start.open_web_interface(opener,interactive=True))
        self.assertEqual(opener.call_count,1)

    def test_launch_context_records_parent_and_posix_group(self):
        context = runtime.launch_context("test-boundary")
        self.assertEqual(context["pid"], runtime.os.getpid())
        self.assertEqual(context["parent_pid"], runtime.os.getppid())
        self.assertEqual(context["launch_mode"], "test-boundary")
        if runtime.os.name != "nt":
            self.assertIn("session_id", context)

if __name__ == "__main__":
    unittest.main()
