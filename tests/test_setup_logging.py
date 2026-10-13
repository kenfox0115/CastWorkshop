import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.setup_logging import run_logged


class SetupLoggingTests(unittest.TestCase):
    def test_real_child_output_and_traceback_are_saved_and_appended(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "scripts" / "setup.py"
            script.parent.mkdir()
            script.write_text(
                "import sys, subprocess\n"
                "print('prompt without newline', end='', flush=True)\n"
                "print('stderr diagnostic', file=sys.stderr)\n"
                "subprocess.run([sys.executable, '-c', \"print('nested output')\"])\n"
                "raise RuntimeError('example failure')\n", encoding="utf-8")
            output = io.StringIO()
            with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(output):
                self.assertEqual(run_logged(script), 1)
                self.assertEqual(run_logged(script), 1)
            log = (Path(folder) / "logs" / "setup.log").read_text(encoding="utf-8")
            for expected in ("prompt without newline", "stderr diagnostic", "nested output",
                             "Traceback", "RuntimeError: example failure", "Exit code: 1"):
                self.assertIn(expected, log)
                self.assertIn(expected, output.getvalue())
            self.assertEqual(log.count("=== Setup started"), 2)

    def test_root_entry_point_logs_and_runs_in_project_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "setup_project.py"
            script.write_text("from pathlib import Path\nprint('cwd=' + str(Path.cwd()))\n", encoding="utf-8")
            with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_logged(script), 0)
            log = (root / "logs/setup.log").read_text(encoding="utf-8")
            self.assertIn("cwd=" + str(root), log)
            self.assertIn("Exit code: 0", log)

    def test_success_does_not_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "scripts" / "setup.py"
            script.parent.mkdir()
            script.write_text("print('success')\n", encoding="utf-8")
            with patch("sys.stdin.isatty", return_value=True), contextlib.redirect_stdout(io.StringIO()), patch("builtins.input") as prompt:
                self.assertEqual(run_logged(script), 0)
                prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
